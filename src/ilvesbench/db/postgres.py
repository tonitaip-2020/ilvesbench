from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from ilvesbench.config import PostgresConfig
from ilvesbench.models import (
    ColumnMetadata,
    ForeignKeyMetadata,
    IndexMetadata,
    SchemaSnapshot,
    TableMetadata,
    UniqueConstraintMetadata,
)

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover - exercised through runtime fallback
    psycopg = None
    sql = None
    dict_row = None


class PostgresToolError(RuntimeError):
    pass


class PostgresInspector:
    def __init__(self, config: PostgresConfig) -> None:
        self._config = config

    def check_connection_status(self) -> dict:
        checks = [
            self._check_database_connection(self._config.original_database, "source database"),
            self._check_database_connection(self._config.admin_database, "admin database"),
        ]
        if any(check["status"] == "failed" for check in checks):
            status = "failed"
        elif any(check.get("hint") for check in checks):
            status = "warning"
        else:
            status = "ok"
        hints = [check["hint"] for check in checks if check.get("hint")]
        return {
            "status": status,
            "host": self._config.host,
            "port": self._config.port,
            "user": self._config.user,
            "original_database": self._config.original_database,
            "new_database": self._config.new_database,
            "admin_database": self._config.admin_database,
            "schemas": self._config.schemas,
            "checks": checks,
            "hint": hints[0] if hints else "",
        }

    def _check_database_connection(self, database: str, label: str) -> dict:
        try:
            conn = self._connect(database)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            current_user AS current_user,
                            current_database() AS current_database,
                            current_setting('server_version') AS server_version,
                            has_database_privilege(current_database(), 'CONNECT') AS can_connect,
                            EXISTS (
                                SELECT 1
                                FROM pg_roles
                                WHERE rolname = current_user
                                  AND (rolcreatedb OR rolsuper)
                            ) AS can_create_database
                        """
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
            return {
                "label": label,
                "database": database,
                "status": "ok",
                "current_user": row["current_user"],
                "current_database": row["current_database"],
                "server_version": row["server_version"],
                "can_connect": bool(row["can_connect"]),
                "can_create_database": bool(row["can_create_database"]),
                "hint": "" if bool(row["can_create_database"]) or database != self._config.admin_database else (
                    "The configured user can connect, but may not be able to create db-new. "
                    "Grant CREATEDB, use a superuser/admin role, or pre-create the target database."
                ),
            }
        except Exception as exc:
            return {
                "label": label,
                "database": database,
                "status": "failed",
                "error": str(exc),
                "hint": self._connection_hint(str(exc)),
            }

    def inspect_schema(self) -> SchemaSnapshot:
        conn = self._connect(self._config.original_database)
        try:
            tables = self._load_tables(conn)
            columns = self._load_columns(conn)
            indexes = self._load_indexes(conn)
            foreign_keys = self._load_foreign_keys(conn)
            unique_constraints = self._load_unique_constraints(conn)
            size_bytes = self._load_database_size(conn)
        finally:
            conn.close()

        table_map: dict[tuple[str, str], TableMetadata] = {}
        for row in tables:
            key = (row["table_schema"], row["table_name"])
            table_map[key] = TableMetadata(schema=row["table_schema"], name=row["table_name"])

        for row in columns:
            key = (row["table_schema"], row["table_name"])
            table_map[key].columns.append(
                ColumnMetadata(
                    name=row["column_name"],
                    data_type=row["data_type"],
                    is_nullable=row["is_nullable"] == "YES",
                    default=row["column_default"],
                )
            )

        for row in indexes:
            key = (row["schema_name"], row["table_name"])
            if key in table_map:
                table_map[key].indexes.append(
                    IndexMetadata(
                        name=row["index_name"],
                        definition=row["index_definition"],
                        is_unique=bool(row["is_unique"]),
                    )
                )

        for row in foreign_keys:
            key = (row["table_schema"], row["table_name"])
            if key in table_map:
                table_map[key].foreign_keys.append(
                    ForeignKeyMetadata(
                        name=row["constraint_name"],
                        columns=list(row["columns"]),
                        referenced_table=f"{row['foreign_table_schema']}.{row['foreign_table_name']}",
                        referenced_columns=list(row["foreign_columns"]),
                    )
                )

        grouped_uniques: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
        for row in unique_constraints:
            group_key = (row["table_schema"], row["table_name"], row["constraint_name"])
            grouped_uniques[group_key].append((row["ordinal_position"], row["column_name"]))
        for (schema_name, table_name, constraint_name), values in grouped_uniques.items():
            key = (schema_name, table_name)
            if key in table_map:
                ordered_columns = [column for _, column in sorted(values)]
                table_map[key].unique_constraints.append(
                    UniqueConstraintMetadata(name=constraint_name, columns=ordered_columns)
                )

        ordered_tables = sorted(table_map.values(), key=lambda item: (item.schema, item.name))
        return SchemaSnapshot(
            database=self._config.original_database,
            collected_at=datetime.now(UTC).isoformat(),
            tables=ordered_tables,
            database_size_bytes=size_bytes,
        )

    def create_database_if_missing(self, database: str) -> str:
        if self.database_exists(database):
            return "exists"

        conn = self._connect(self._config.admin_database, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{database}"')
        finally:
            conn.close()
        return "created"

    def drop_database_if_exists(self, database: str) -> str:
        if not self.database_exists(database):
            return "missing"

        conn = self._connect(self._config.admin_database, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database,),
                )
                cur.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(database)))
        finally:
            conn.close()
        return "dropped"

    def database_exists(self, database: str) -> bool:
        conn = self._connect(self._config.admin_database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def execute_statements(self, database: str, statements: list[str]) -> list[dict]:
        conn = self._connect(database)
        executed: list[dict] = []
        try:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
                    executed.append({"statement": statement})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return executed

    def ensure_source_fdw(
        self,
        target_database: str,
        source_schema_alias: str,
        source_tables: list[TableMetadata],
    ) -> list[str]:
        conn = self._connect(target_database)
        server_name = "ilvesbench_source_server"
        created_tables: list[str] = []
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
                cur.execute(
                    sql.SQL(
                        """
                        CREATE SERVER IF NOT EXISTS {server_name}
                        FOREIGN DATA WRAPPER postgres_fdw
                        OPTIONS (
                            host {host},
                            port {port},
                            dbname {dbname}
                        )
                        """
                    ).format(
                        server_name=sql.Identifier(server_name),
                        host=sql.Literal(self._config.host),
                        port=sql.Literal(str(self._config.port)),
                        dbname=sql.Literal(self._config.original_database),
                    )
                )
                cur.execute(
                    sql.SQL(
                        """
                        CREATE USER MAPPING IF NOT EXISTS FOR CURRENT_USER
                        SERVER {server_name}
                        OPTIONS (
                            user {user_name},
                            password {password}
                        )
                        """
                    ).format(
                        server_name=sql.Identifier(server_name),
                        user_name=sql.Literal(self._config.user),
                        password=sql.Literal(self._config.password),
                    )
                )
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{source_schema_alias}"')

                for table in source_tables:
                    foreign_table_name = table.name
                    column_defs = []
                    for column in table.columns:
                        null_sql = "" if column.is_nullable else " NOT NULL"
                        column_defs.append(f'"{column.name}" {column.data_type.upper()}{null_sql}')
                    columns_sql = ", ".join(column_defs)
                    cur.execute(
                        sql.SQL(
                            """
                            CREATE FOREIGN TABLE IF NOT EXISTS {schema_name}.{foreign_table_name} (
                                {columns}
                            )
                            SERVER {server_name}
                            OPTIONS (
                                schema_name {remote_schema},
                                table_name {remote_table}
                            )
                            """
                        ).format(
                            schema_name=sql.Identifier(source_schema_alias),
                            foreign_table_name=sql.Identifier(foreign_table_name),
                            columns=sql.SQL(columns_sql),
                            server_name=sql.Identifier(server_name),
                            remote_schema=sql.Literal(table.schema),
                            remote_table=sql.Literal(table.name),
                        )
                    )
                    created_tables.append(f"{source_schema_alias}.{foreign_table_name}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return created_tables

    def _connect(self, database: str, autocommit: bool = False):
        if psycopg is None:
            raise PostgresToolError(
                "psycopg is not installed. Install the optional dependency with "
                "`pip install \"psycopg[binary]>=3.2,<4\"`."
            )

        conn = psycopg.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            dbname=database,
            connect_timeout=self._config.connect_timeout_seconds,
            row_factory=dict_row,
        )
        conn.autocommit = autocommit
        return conn

    def _connection_hint(self, message: str) -> str:
        normalized = message.lower()
        if "psycopg is not installed" in normalized:
            return "Install psycopg in the host virtual environment, then restart IlvesBench."
        if "password authentication failed" in normalized or "authentication failed" in normalized:
            return "Check the PostgreSQL user/password in the TOML file and confirm the role exists in PostgreSQL."
        if "no pg_hba.conf entry" in normalized or "pg_hba" in normalized:
            return "Check pg_hba.conf and make sure the Docker PostgreSQL instance allows this host/user/database combination."
        if "connection refused" in normalized or "could not connect" in normalized or "is the server running" in normalized:
            return "Check that the PostgreSQL container is running and that Docker publishes the configured host/port."
        if "timeout" in normalized or "timed out" in normalized:
            return "Check the TOML host/port and Docker port mapping; the connection attempt timed out."
        if "database" in normalized and "does not exist" in normalized:
            return "Check original_database/admin_database in the TOML file, or create the missing PostgreSQL database."
        if "permission denied" in normalized or "insufficient privilege" in normalized or "must be owner" in normalized:
            return "Check PostgreSQL role privileges. The configured user needs read access to db-original and permission for approved db-new actions."
        return "Check the PostgreSQL settings in the TOML file, Docker port mapping, pg_hba.conf, and role permissions."

    def _load_tables(self, conn) -> list[dict]:
        query = """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema = ANY(%s)
            ORDER BY table_schema, table_name
        """
        with conn.cursor() as cur:
            cur.execute(query, (self._config.schemas,))
            return list(cur.fetchall())

    def _load_columns(self, conn) -> list[dict]:
        query = """
            SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = ANY(%s)
            ORDER BY table_schema, table_name, ordinal_position
        """
        with conn.cursor() as cur:
            cur.execute(query, (self._config.schemas,))
            return list(cur.fetchall())

    def _load_indexes(self, conn) -> list[dict]:
        query = """
            SELECT schemaname AS schema_name,
                   tablename AS table_name,
                   indexname AS index_name,
                   indexdef AS index_definition,
                   indexdef ILIKE 'CREATE UNIQUE INDEX%%' AS is_unique
            FROM pg_indexes
            WHERE schemaname = ANY(%s)
            ORDER BY schemaname, tablename, indexname
        """
        with conn.cursor() as cur:
            cur.execute(query, (self._config.schemas,))
            return list(cur.fetchall())

    def _load_foreign_keys(self, conn) -> list[dict]:
        query = """
            SELECT
                tc.table_schema,
                tc.table_name,
                tc.constraint_name,
                array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS columns,
                ccu.table_schema AS foreign_table_schema,
                ccu.table_name AS foreign_table_name,
                array_agg(ccu.column_name ORDER BY kcu.ordinal_position) AS foreign_columns
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = ANY(%s)
            GROUP BY tc.table_schema, tc.table_name, tc.constraint_name, ccu.table_schema, ccu.table_name
            ORDER BY tc.table_schema, tc.table_name, tc.constraint_name
        """
        with conn.cursor() as cur:
            cur.execute(query, (self._config.schemas,))
            return list(cur.fetchall())

    def _load_unique_constraints(self, conn) -> list[dict]:
        query = """
            SELECT
                tc.table_schema,
                tc.table_name,
                tc.constraint_name,
                kcu.column_name,
                kcu.ordinal_position
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
              AND tc.table_schema = ANY(%s)
            ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position
        """
        with conn.cursor() as cur:
            cur.execute(query, (self._config.schemas,))
            return list(cur.fetchall())

    def _load_database_size(self, conn) -> int | None:
        query = "SELECT pg_database_size(current_database()) AS size_bytes"
        with conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
        return row["size_bytes"] if row else None
