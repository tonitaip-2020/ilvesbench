from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
import json
import re

from ilvesbench.benchmark.normal_form import FirstNormalFormFinding, FirstNormalFormScanner
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

    def discover_databases(self) -> list[dict]:
        conn = self._connect(self._config.admin_database)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        d.datname AS name,
                        pg_catalog.pg_get_userbyid(d.datdba) AS owner,
                        pg_database_size(d.datname) AS size_bytes,
                        has_database_privilege(d.datname, 'CONNECT') AS can_connect
                    FROM pg_database AS d
                    WHERE d.datistemplate = false
                      AND d.datallowconn = true
                      AND d.datname <> 'postgres'
                    ORDER BY d.datname
                    """
                )
                rows = [
                    json.loads(json.dumps(dict(row), ensure_ascii=True, default=str))
                    for row in cur.fetchall()
                ]
        finally:
            conn.close()

        databases: list[dict] = []
        for row in rows:
            name = str(row.get("name", ""))
            table_count: int | None = None
            schema_names: list[str] = []
            connect_error = ""
            if row.get("can_connect"):
                try:
                    table_count, schema_names = self._discover_database_shape(name)
                except Exception as exc:
                    connect_error = str(exc)
            databases.append(
                {
                    "name": name,
                    "owner": row.get("owner"),
                    "size_bytes": row.get("size_bytes"),
                    "can_connect": bool(row.get("can_connect")) and not connect_error,
                    "table_count": table_count,
                    "schemas": schema_names,
                    "error": connect_error,
                    "is_current_original": name == self._config.original_database,
                    "is_current_target": name == self._config.new_database,
                }
            )
        return databases

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

    def profile_database(self, database: str) -> dict:
        if not self.database_exists(database):
            return {
                "database": database,
                "status": "missing",
                "summary": "Database does not exist yet.",
            }

        conn = self._connect(database)
        try:
            information_schema = self._load_information_schema_profile(conn)
            table_metrics = self._load_table_metrics(conn)
            return self._build_abstract_profile(database, information_schema, table_metrics)
        finally:
            conn.close()

    def _discover_database_shape(self, database: str) -> tuple[int, list[str]]:
        conn = self._connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS table_count
                    FROM information_schema.tables
                    WHERE table_type = 'BASE TABLE'
                      AND table_schema NOT IN ('information_schema', 'pg_catalog')
                      AND table_schema NOT LIKE 'pg_%'
                    """
                )
                table_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name NOT IN ('information_schema', 'pg_catalog')
                      AND schema_name NOT LIKE 'pg_%'
                    ORDER BY schema_name
                    """
                )
                schemas = [str(row["schema_name"]) for row in cur.fetchall()]
        finally:
            conn.close()
        return int(table_row["table_count"] or 0) if table_row else 0, schemas

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
                    f"The configured user can connect, but may not be able to create {self._config.new_database}. "
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

    def inspect_schema(self, database: str | None = None) -> SchemaSnapshot:
        database_name = database or self._config.original_database
        conn = self._connect(database_name)
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
            database=database_name,
            collected_at=datetime.now(UTC).isoformat(),
            tables=ordered_tables,
            database_size_bytes=size_bytes,
        )

    def scan_first_normal_form_warnings(
        self,
        schema: SchemaSnapshot,
        sample_limit: int = 250,
    ) -> dict:
        scanner = FirstNormalFormScanner()
        findings = scanner.schema_findings(schema)
        conn = self._connect(schema.database)
        try:
            for finding in findings:
                if finding.pattern != "collection_typed_column":
                    continue
                evidence = self._load_collection_column_evidence(
                    conn,
                    finding.table,
                    finding.column,
                    str(finding.evidence.get("data_type", "")),
                    sample_limit,
                )
                finding.evidence.update(evidence)
                sampled_rows = int(evidence.get("sampled_non_null_rows", 0) or 0)
                multi_value_rows = int(evidence.get("multi_value_rows", 0) or 0)
                if sampled_rows and multi_value_rows:
                    ratio = multi_value_rows / sampled_rows
                    finding.confidence = round(min(0.98, 0.86 + (0.12 * ratio)), 2)
                    finding.summary = (
                        f"{finding.table}.{finding.column} uses {finding.evidence['data_type']} and stores multiple "
                        f"values in {round(ratio * 100, 1)}% of {sampled_rows} sampled non-null rows."
                    )
            for table in schema.tables:
                for column in table.columns:
                    if not scanner.should_sample_column(column.data_type):
                        continue
                    values = self._sample_column_values(conn, table.schema, table.name, column.name, sample_limit)
                    finding = scanner.analyze_column_values(
                        f"{table.schema}.{table.name}",
                        column.name,
                        column.data_type,
                        values,
                    )
                    if finding is None:
                        continue
                    self._attach_candidate_reference(conn, schema, scanner, finding)
                    findings.append(finding)
        finally:
            conn.close()

        scan = scanner.build_scan(findings)
        return {
            "status": scan.status,
            "summary": scan.summary,
            "source": scan.source,
            "sample_limit": sample_limit,
            "finding_count": len(scan.findings),
            "findings": [
                {
                    "table": finding.table,
                    "column": finding.column,
                    "severity": finding.severity,
                    "confidence": finding.confidence,
                    "pattern": finding.pattern,
                    "summary": finding.summary,
                    "recommendation": finding.recommendation,
                    "evidence": finding.evidence,
                    "candidate_reference": finding.candidate_reference,
                }
                for finding in scan.findings
            ],
        }

    def _load_collection_column_evidence(
        self,
        conn,
        qualified_table: str,
        column_name: str,
        data_type: str,
        sample_limit: int,
    ) -> dict:
        if "." not in qualified_table or not data_type.lower().strip().endswith("[]"):
            return {"collection_kind": "other"}
        schema_name, table_name = qualified_table.split(".", 1)
        query = sql.SQL(
            """
            WITH sampled AS (
                SELECT {column} AS value
                FROM {schema}.{table}
                WHERE {column} IS NOT NULL
                LIMIT {sample_limit}
            )
            SELECT
                COUNT(*) AS sampled_non_null_rows,
                COUNT(*) FILTER (WHERE cardinality(value) > 1) AS multi_value_rows,
                MIN(cardinality(value)) AS min_cardinality,
                MAX(cardinality(value)) AS max_cardinality,
                ARRAY(
                    SELECT DISTINCT value::text
                    FROM sampled
                    ORDER BY value::text
                    LIMIT 8
                ) AS value_samples
            FROM sampled
            """
        ).format(
            column=sql.Identifier(column_name),
            schema=sql.Identifier(schema_name),
            table=sql.Identifier(table_name),
            sample_limit=sql.Literal(sample_limit),
        )
        with conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone() or {}
        sampled_rows = int(row.get("sampled_non_null_rows", 0) or 0)
        multi_value_rows = int(row.get("multi_value_rows", 0) or 0)
        return {
            "collection_kind": "array",
            "sampled_non_null_rows": sampled_rows,
            "multi_value_rows": multi_value_rows,
            "multi_value_ratio": round(multi_value_rows / sampled_rows, 4) if sampled_rows else 0.0,
            "min_cardinality": row.get("min_cardinality"),
            "max_cardinality": row.get("max_cardinality"),
            "value_samples": list(row.get("value_samples") or []),
        }

    def validate_workload_statements(self, database: str, statements: list[str]) -> list[dict]:
        conn = self._connect(database)
        errors: list[dict] = []
        try:
            with conn.cursor() as cur:
                for index, statement in enumerate(statements):
                    sql_text = statement.strip().rstrip(";")
                    if not sql_text:
                        continue
                    kind = self._statement_kind(sql_text)
                    try:
                        cur.execute("SET LOCAL statement_timeout = '5s'")
                        cur.execute("SET LOCAL lock_timeout = '2s'")
                        if kind in {"select", "with"}:
                            cur.execute(
                                f"SELECT * FROM ({sql_text}) AS ilvesbench_validation_sample LIMIT 1"
                            )
                            cur.fetchone()
                        elif kind in {"insert", "update", "delete"}:
                            cur.execute(sql_text)
                        else:
                            raise ValueError(
                                "Rewritten workload must contain SELECT, WITH, INSERT, UPDATE, or DELETE statements only."
                            )
                    except Exception as exc:
                        conn.rollback()
                        errors.append({"index": index, "statement": statement, "kind": kind, "error": str(exc)})
                    else:
                        conn.rollback()
        finally:
            conn.close()
        return errors

    def validate_functional_dependency(
        self,
        database: str,
        schema_name: str,
        table_name: str,
        determinant_columns: list[str],
        dependent_columns: list[str],
    ) -> dict:
        if not determinant_columns or not dependent_columns:
            return {
                "status": "skipped",
                "summary": "Functional dependency validation requires determinant and dependent columns.",
            }
        if psycopg is None or sql is None:
            return {
                "status": "skipped",
                "summary": "psycopg is not available, so source-data FD validation was skipped.",
            }

        conn = self._connect(database)
        try:
            with conn.cursor() as cur:
                determinant_sql = sql.SQL(", ").join(sql.Identifier(column) for column in determinant_columns)
                dependent_parts = [
                    sql.SQL("COALESCE({}::text, '<NULL>')").format(sql.Identifier(column))
                    for column in dependent_columns
                ]
                dependent_expr = sql.SQL("concat_ws(chr(31), {})").format(sql.SQL(", ").join(dependent_parts))
                query = sql.SQL(
                    """
                    SELECT COUNT(*) AS violation_count
                    FROM (
                        SELECT {determinants}
                        FROM {table}
                        GROUP BY {determinants}
                        HAVING COUNT(DISTINCT {dependent_expr}) > 1
                        LIMIT 1
                    ) AS ilvesbench_fd_violations
                    """
                ).format(
                    determinants=determinant_sql,
                    table=sql.Identifier(schema_name, table_name),
                    dependent_expr=dependent_expr,
                )
                cur.execute("SET LOCAL statement_timeout = '10s'")
                cur.execute(query)
                row = cur.fetchone()
        except Exception as exc:
            conn.rollback()
            return {
                "status": "skipped",
                "summary": "Functional dependency validation could not run against the source data.",
                "error": str(exc),
                "database": database,
                "table": f"{schema_name}.{table_name}",
            }
        finally:
            conn.close()

        violation_count = int(row["violation_count"] or 0) if row else 0
        if violation_count:
            return {
                "status": "violated",
                "summary": "Current source data contradicts this candidate functional dependency.",
                "violation_count": violation_count,
                "database": database,
                "table": f"{schema_name}.{table_name}",
            }
        return {
            "status": "consistent",
            "summary": "No contradiction to this candidate functional dependency was found in current source data.",
            "violation_count": 0,
            "database": database,
            "table": f"{schema_name}.{table_name}",
        }

    def compare_query_results(
        self,
        original_database: str,
        target_database: str,
        original_statement: str,
        rewritten_statement: str,
        *,
        row_limit: int = 100,
        random_seed: float = 0.314159,
    ) -> dict:
        if not self._is_result_query(original_statement) or not self._is_result_query(rewritten_statement):
            return {
                "status": "skipped",
                "reason": "Result comparison currently runs SELECT/WITH queries only.",
                "original_row_count": None,
                "rewritten_row_count": None,
                "row_limit": row_limit,
                "random_seed": random_seed,
            }

        original_rows = self._fetch_query_sample(
            original_database,
            original_statement,
            row_limit=row_limit,
            random_seed=random_seed,
        )
        rewritten_rows = self._fetch_query_sample(
            target_database,
            rewritten_statement,
            row_limit=row_limit,
            random_seed=random_seed,
        )
        original_canonical = [self._canonical_row(row) for row in original_rows]
        rewritten_canonical = [self._canonical_row(row) for row in rewritten_rows]
        exact_order_match = original_canonical == rewritten_canonical
        unordered_match = sorted(original_canonical) == sorted(rewritten_canonical)
        return {
            "status": "passed" if exact_order_match or unordered_match else "failed",
            "exact_order_match": exact_order_match,
            "unordered_match": unordered_match,
            "original_row_count": len(original_rows),
            "rewritten_row_count": len(rewritten_rows),
            "row_limit": row_limit,
            "random_seed": random_seed,
            "original_sample": original_rows[:10],
            "rewritten_sample": rewritten_rows[:10],
        }

    def _fetch_query_sample(
        self,
        database: str,
        statement: str,
        *,
        row_limit: int,
        random_seed: float,
    ) -> list[dict]:
        conn = self._connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '15s'")
                cur.execute("SELECT setseed(%s)", (random_seed,))
                cur.execute(
                    f"SELECT * FROM ({statement.strip().rstrip(';')}) AS ilvesbench_validation_sample LIMIT %s",
                    (row_limit,),
                )
                rows = [dict(row) for row in cur.fetchall()]
            conn.rollback()
            return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _canonical_row(self, row: dict) -> str:
        return json.dumps(row, sort_keys=True, ensure_ascii=True, default=str)

    def _is_result_query(self, statement: str) -> bool:
        return bool(re.match(r"^\s*(WITH|SELECT)\b", statement, re.IGNORECASE))

    def _statement_kind(self, statement: str) -> str:
        text = statement.strip()
        while True:
            stripped = re.sub(r"^\s*--[^\n]*(?:\n|$)", "", text, count=1)
            stripped = re.sub(r"^\s*/\*.*?\*/", "", stripped, count=1, flags=re.DOTALL)
            if stripped == text:
                break
            text = stripped.strip()
        match = re.match(r"^([A-Za-z]+)\b", text)
        return match.group(1).lower() if match else "unknown"

    def _sample_column_values(
        self,
        conn,
        schema_name: str,
        table_name: str,
        column_name: str,
        sample_limit: int,
    ) -> list[str]:
        query = sql.SQL(
            """
            SELECT {column_name}::text AS value
            FROM {schema_name}.{table_name}
            WHERE {column_name} IS NOT NULL
              AND length({column_name}::text) > 0
            LIMIT {sample_limit}
            """
        ).format(
            column_name=sql.Identifier(column_name),
            schema_name=sql.Identifier(schema_name),
            table_name=sql.Identifier(table_name),
            sample_limit=sql.Literal(sample_limit),
        )
        with conn.cursor() as cur:
            cur.execute(query)
            return [str(row["value"]) for row in cur.fetchall()]

    def _attach_candidate_reference(
        self,
        conn,
        schema: SchemaSnapshot,
        scanner: FirstNormalFormScanner,
        finding: FirstNormalFormFinding,
    ) -> None:
        tokens = finding.evidence.get("token_samples", [])
        if not tokens:
            return
        token_sample = [str(token) for token in tokens[:80] if str(token)]
        if len(token_sample) < 2:
            return

        best_reference: dict | None = None
        for table, column_name in scanner.candidate_key_columns(schema):
            candidate_table = f"{table.schema}.{table.name}"
            if candidate_table == finding.table and column_name == finding.column:
                continue
            try:
                match_count = self._count_matching_tokens(conn, table.schema, table.name, column_name, token_sample)
            except Exception:
                continue
            match_ratio = match_count / len(token_sample)
            if match_count < 2 or match_ratio < 0.25:
                continue
            candidate = {
                "table": candidate_table,
                "column": column_name,
                "matched_sample_tokens": match_count,
                "sample_token_count": len(token_sample),
                "match_ratio": round(match_ratio, 4),
            }
            if best_reference is None or candidate["match_ratio"] > best_reference["match_ratio"]:
                best_reference = candidate

        if best_reference is not None:
            finding.candidate_reference = best_reference
            finding.confidence = round(min(0.98, finding.confidence + 0.08), 2)
            finding.evidence["candidate_reference_detected"] = True
            finding.recommendation = (
                f"Consider a child relation from {finding.table} to "
                f"{best_reference['table']}.{best_reference['column']} with one row per extracted token."
            )

    def _count_matching_tokens(
        self,
        conn,
        schema_name: str,
        table_name: str,
        column_name: str,
        tokens: list[str],
    ) -> int:
        query = sql.SQL(
            """
            SELECT COUNT(DISTINCT {column_name}) AS match_count
            FROM {schema_name}.{table_name}
            WHERE {column_name} = ANY(%s)
            """
        ).format(
            column_name=sql.Identifier(column_name),
            schema_name=sql.Identifier(schema_name),
            table_name=sql.Identifier(table_name),
        )
        with conn.cursor() as cur:
            cur.execute(query, (tokens,))
            row = cur.fetchone()
        return int(row["match_count"] or 0) if row else 0

    def collect_database_metrics(self, database: str) -> dict:
        conn = self._connect(database)
        try:
            return {
                "database": database,
                "collected_at": datetime.now(UTC).isoformat(),
                "database_size_bytes": self._load_database_size(conn),
                "table_metrics": self._load_table_metrics(conn),
                "database_activity": self._load_database_activity(conn),
                "statement_metrics": self._load_statement_metrics(conn),
            }
        finally:
            conn.close()

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

    def truncate_user_tables(self, database: str) -> list[str]:
        conn = self._connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_schema, table_name
                    FROM information_schema.tables
                    WHERE table_type = 'BASE TABLE'
                      AND table_schema NOT IN ('information_schema', 'pg_catalog')
                      AND table_schema NOT LIKE 'pg_%'
                    ORDER BY table_schema, table_name
                    """
                )
                tables = [(str(row["table_schema"]), str(row["table_name"])) for row in cur.fetchall()]
                if not tables:
                    return []
                table_sql = sql.SQL(", ").join(
                    sql.SQL("{}.{}").format(sql.Identifier(schema_name), sql.Identifier(table_name))
                    for schema_name, table_name in tables
                )
                cur.execute(
                    sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(table_sql)
                )
            conn.commit()
            return [f"{schema_name}.{table_name}" for schema_name, table_name in tables]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
            return (
                "Check PostgreSQL role privileges. The configured user needs read access to "
                f"{self._config.original_database} and permission for approved {self._config.new_database} actions."
            )
        return "Check the PostgreSQL settings in the TOML file, Docker port mapping, pg_hba.conf, and role permissions."

    def _load_tables(self, conn) -> list[dict]:
        query = """
            SELECT
                n.nspname AS table_schema,
                c.relname AS table_name
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname = ANY(%s)
            ORDER BY n.nspname, c.relname
        """
        with conn.cursor() as cur:
            cur.execute(query, (self._config.schemas,))
            return list(cur.fetchall())

    def _load_columns(self, conn) -> list[dict]:
        query = """
            SELECT
                n.nspname AS table_schema,
                c.relname AS table_name,
                a.attname AS column_name,
                pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
                pg_get_expr(ad.adbin, ad.adrelid) AS column_default
            FROM pg_attribute AS a
            JOIN pg_class AS c ON c.oid = a.attrelid
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef AS ad ON ad.adrelid = c.oid AND ad.adnum = a.attnum
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname = ANY(%s)
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attnum
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
                ns.nspname AS table_schema,
                rel.relname AS table_name,
                con.conname AS constraint_name,
                array_agg(att.attname ORDER BY keys.ordinality) AS columns,
                foreign_ns.nspname AS foreign_table_schema,
                foreign_rel.relname AS foreign_table_name,
                array_agg(foreign_att.attname ORDER BY keys.ordinality) AS foreign_columns
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS ns ON ns.oid = rel.relnamespace
            JOIN pg_catalog.pg_class AS foreign_rel ON foreign_rel.oid = con.confrelid
            JOIN pg_catalog.pg_namespace AS foreign_ns ON foreign_ns.oid = foreign_rel.relnamespace
            CROSS JOIN LATERAL unnest(con.conkey, con.confkey)
              WITH ORDINALITY AS keys(attnum, foreign_attnum, ordinality)
            JOIN pg_catalog.pg_attribute AS att
              ON att.attrelid = con.conrelid AND att.attnum = keys.attnum
            JOIN pg_catalog.pg_attribute AS foreign_att
              ON foreign_att.attrelid = con.confrelid AND foreign_att.attnum = keys.foreign_attnum
            WHERE con.contype = 'f'
              AND ns.nspname = ANY(%s)
            GROUP BY ns.nspname, rel.relname, con.conname, foreign_ns.nspname, foreign_rel.relname
            ORDER BY ns.nspname, rel.relname, con.conname
        """
        with conn.cursor() as cur:
            cur.execute(query, (self._config.schemas,))
            return list(cur.fetchall())

    def _load_unique_constraints(self, conn) -> list[dict]:
        query = """
            SELECT
                ns.nspname AS table_schema,
                rel.relname AS table_name,
                con.conname AS constraint_name,
                att.attname AS column_name,
                keys.ordinality AS ordinal_position
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS ns ON ns.oid = rel.relnamespace
            CROSS JOIN LATERAL unnest(con.conkey)
              WITH ORDINALITY AS keys(attnum, ordinality)
            JOIN pg_catalog.pg_attribute AS att
              ON att.attrelid = con.conrelid AND att.attnum = keys.attnum
            WHERE con.contype IN ('p', 'u')
              AND ns.nspname = ANY(%s)
            ORDER BY ns.nspname, rel.relname, con.conname, keys.ordinality
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

    def _load_information_schema_profile(self, conn) -> dict:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.table_schema,
                    t.table_name,
                    COUNT(c.column_name) AS column_count,
                    COUNT(*) FILTER (WHERE c.is_nullable = 'YES') AS nullable_column_count,
                    COUNT(*) FILTER (WHERE c.column_default IS NOT NULL) AS defaulted_column_count
                FROM information_schema.tables AS t
                LEFT JOIN information_schema.columns AS c
                  ON c.table_schema = t.table_schema
                 AND c.table_name = t.table_name
                WHERE t.table_type = 'BASE TABLE'
                  AND t.table_schema = ANY(%s)
                GROUP BY t.table_schema, t.table_name
                ORDER BY t.table_schema, t.table_name
                """,
                (self._config.schemas,),
            )
            tables = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    c.table_schema,
                    c.table_name,
                    c.data_type,
                    COUNT(*) AS count
                FROM information_schema.columns AS c
                JOIN information_schema.tables AS t
                  ON t.table_schema = c.table_schema
                 AND t.table_name = c.table_name
                 AND t.table_type = 'BASE TABLE'
                WHERE c.table_schema = ANY(%s)
                GROUP BY c.table_schema, c.table_name, c.data_type
                """,
                (self._config.schemas,),
            )
            data_types = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    tc.table_schema,
                    tc.table_name,
                    tc.constraint_name,
                    tc.constraint_type,
                    ccu.table_schema AS referenced_table_schema,
                    ccu.table_name AS referenced_table_name
                FROM information_schema.table_constraints AS tc
                LEFT JOIN information_schema.constraint_column_usage AS ccu
                  ON ccu.constraint_schema = tc.constraint_schema
                 AND ccu.constraint_name = tc.constraint_name
                WHERE tc.table_schema = ANY(%s)
                """,
                (self._config.schemas,),
            )
            constraints = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT COUNT(*) AS index_count
                FROM pg_indexes
                WHERE schemaname = ANY(%s)
                """,
                (self._config.schemas,),
            )
            index_row = cur.fetchone()

        return {
            "tables": tables,
            "data_types": data_types,
            "constraints": constraints,
            "index_count": int(index_row["index_count"] or 0) if index_row else 0,
        }

    def _build_abstract_profile(self, database: str, information_schema: dict, table_metrics: list[dict]) -> dict:
        tables = information_schema.get("tables", [])
        constraints = information_schema.get("constraints", [])
        data_types = information_schema.get("data_types", [])
        table_keys = {(row["table_schema"], row["table_name"]) for row in tables}
        primary_key_tables = {
            (row["table_schema"], row["table_name"])
            for row in constraints
            if row.get("constraint_type") == "PRIMARY KEY"
        }
        fk_rows = [row for row in constraints if row.get("constraint_type") == "FOREIGN KEY"]
        unique_count = sum(1 for row in constraints if row.get("constraint_type") == "UNIQUE")
        check_count = sum(1 for row in constraints if row.get("constraint_type") == "CHECK")
        index_count = int(information_schema.get("index_count") or 0)
        column_counts = [int(row.get("column_count") or 0) for row in tables]
        total_columns = sum(column_counts)
        wide_tables = sum(1 for count in column_counts if count >= 30)
        missing_pk = max(0, len(table_keys - primary_key_tables))
        nullable_columns = sum(int(row.get("nullable_column_count") or 0) for row in tables)
        defaulted_columns = sum(int(row.get("defaulted_column_count") or 0) for row in tables)
        type_counts: dict[str, int] = defaultdict(int)
        for row in data_types:
            type_counts[str(row.get("data_type") or "unknown")] += int(row.get("count") or 0)
        estimated_rows = sum(int(row.get("n_live_tup") or 0) for row in table_metrics)
        populated_tables = sum(1 for row in table_metrics if int(row.get("n_live_tup") or 0) > 0)

        graph: dict[tuple[str, str], set[tuple[str, str]]] = {key: set() for key in table_keys}
        for row in fk_rows:
            source = (row["table_schema"], row["table_name"])
            target_schema = row.get("referenced_table_schema")
            target_table = row.get("referenced_table_name")
            if not target_schema or not target_table:
                continue
            target = (target_schema, target_table)
            if source in graph and target in graph:
                graph[source].add(target)
                graph[target].add(source)

        clusters = self._abstract_clusters(graph, tables)
        total_bytes = sum(int(row.get("total_bytes") or 0) for row in table_metrics)
        table_size_buckets = self._table_size_buckets(table_metrics)
        issue_count = missing_pk + wide_tables
        if len(tables) > 200:
            scale = "large"
        elif len(tables) > 40:
            scale = "medium"
        else:
            scale = "small"

        return {
            "database": database,
            "status": "completed",
            "collected_at": datetime.now(UTC).isoformat(),
            "scale": scale,
            "summary": f"{len(tables)} tables, {total_columns} columns, {len(fk_rows)} foreign-key relationships.",
            "counts": {
                "tables": len(tables),
                "columns": total_columns,
                "foreign_keys": len(fk_rows),
                "indexes": index_count,
                "unique_constraints": unique_count,
                "check_constraints": check_count,
                "tables_without_primary_key": missing_pk,
                "wide_tables": wide_tables,
                "estimated_rows": estimated_rows,
                "populated_tables": populated_tables,
                "nullable_columns": nullable_columns,
                "defaulted_columns": defaulted_columns,
            },
            "averages": {
                "columns_per_table": round(total_columns / len(tables), 2) if tables else 0,
                "foreign_keys_per_table": round(len(fk_rows) / len(tables), 2) if tables else 0,
            },
            "risk_signals": [
                {"label": "Tables without primary-key evidence", "value": missing_pk, "severity": "warning" if missing_pk else "ok"},
                {"label": "Wide tables", "value": wide_tables, "severity": "warning" if wide_tables else "ok"},
                {"label": "Relationship density", "value": round(len(fk_rows) / len(tables), 2) if tables else 0, "severity": "info"},
            ],
            "type_mix": [
                {"data_type": key, "count": value}
                for key, value in sorted(type_counts.items(), key=lambda item: item[1], reverse=True)[:8]
            ],
            "storage": {
                "total_relation_bytes": total_bytes,
                "table_size_buckets": table_size_buckets,
            },
            "clusters": clusters,
            "privacy": {
                "abstracted": True,
                "table_names_returned": False,
                "column_names_returned": False,
            },
            "issue_count": issue_count,
        }

    def _abstract_clusters(self, graph: dict[tuple[str, str], set[tuple[str, str]]], tables: list[dict]) -> list[dict]:
        column_counts = {
            (row["table_schema"], row["table_name"]): int(row.get("column_count") or 0)
            for row in tables
        }
        seen: set[tuple[str, str]] = set()
        clusters: list[dict] = []
        for node in sorted(graph):
            if node in seen:
                continue
            stack = [node]
            component: list[tuple[str, str]] = []
            seen.add(node)
            while stack:
                current = stack.pop()
                component.append(current)
                for neighbor in graph[current]:
                    if neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)

            relationship_edges = sum(len(graph[item]) for item in component) // 2
            component_columns = sum(column_counts.get(item, 0) for item in component)
            clusters.append(
                {
                    "label": f"Cluster {len(clusters) + 1}",
                    "table_count": len(component),
                    "relationship_count": relationship_edges,
                    "average_columns": round(component_columns / len(component), 2) if component else 0,
                    "shape": self._cluster_shape(len(component), relationship_edges),
                }
            )
        return sorted(clusters, key=lambda item: item["table_count"], reverse=True)[:12]

    def _cluster_shape(self, table_count: int, relationship_count: int) -> str:
        if table_count <= 1:
            return "isolated"
        density = relationship_count / table_count
        if density >= 2:
            return "dense"
        if density >= 1:
            return "connected"
        return "sparse"

    def _table_size_buckets(self, table_metrics: list[dict]) -> dict:
        buckets = {"empty_or_tiny": 0, "small": 0, "medium": 0, "large": 0}
        for row in table_metrics:
            size = int(row.get("total_bytes") or 0)
            if size < 1024 * 1024:
                buckets["empty_or_tiny"] += 1
            elif size < 128 * 1024 * 1024:
                buckets["small"] += 1
            elif size < 1024 * 1024 * 1024:
                buckets["medium"] += 1
            else:
                buckets["large"] += 1
        return buckets

    def _load_table_metrics(self, conn) -> list[dict]:
        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                pg_total_relation_size(c.oid) AS total_bytes,
                pg_relation_size(c.oid) AS heap_bytes,
                pg_indexes_size(c.oid) AS index_bytes,
                COALESCE(s.seq_scan, 0) AS seq_scan,
                COALESCE(s.idx_scan, 0) AS idx_scan,
                CASE
                    WHEN COALESCE(s.n_live_tup, 0) > 0 THEN COALESCE(s.n_live_tup, 0)
                    WHEN c.reltuples > 0 THEN c.reltuples::bigint
                    WHEN pg_relation_size(c.oid) > 0 THEN GREATEST(1, (pg_relation_size(c.oid) / 200)::bigint)
                    ELSE 0
                END AS n_live_tup,
                COALESCE(s.n_live_tup, 0) AS stats_live_tup,
                CASE WHEN c.reltuples > 0 THEN c.reltuples::bigint ELSE 0 END AS planner_rows,
                CASE
                    WHEN COALESCE(s.n_live_tup, 0) > 0 THEN 'pg_stat_user_tables'
                    WHEN c.reltuples > 0 THEN 'pg_class_reltuples'
                    WHEN pg_relation_size(c.oid) > 0 THEN 'relation_size_heuristic'
                    ELSE 'none'
                END AS row_estimate_source,
                COALESCE(s.n_dead_tup, 0) AS n_dead_tup
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables AS s ON s.relid = c.oid
            WHERE c.relkind = 'r'
              AND n.nspname = ANY(%s)
            ORDER BY pg_total_relation_size(c.oid) DESC, n.nspname, c.relname
        """
        with conn.cursor() as cur:
            cur.execute(query, (self._config.schemas,))
            return [dict(row) for row in cur.fetchall()]

    def _load_database_activity(self, conn) -> dict:
        query = """
            SELECT
                numbackends,
                xact_commit,
                xact_rollback,
                blks_read,
                blks_hit,
                tup_returned,
                tup_fetched,
                tup_inserted,
                tup_updated,
                tup_deleted,
                temp_bytes,
                deadlocks
            FROM pg_stat_database
            WHERE datname = current_database()
        """
        with conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
        payload = dict(row) if row else {}
        blocks_read = int(payload.get("blks_read") or 0)
        blocks_hit = int(payload.get("blks_hit") or 0)
        total_blocks = blocks_read + blocks_hit
        payload["cache_hit_ratio"] = round(blocks_hit / total_blocks, 6) if total_blocks else None
        return payload

    def _load_statement_metrics(self, conn) -> dict:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements') AS enabled")
                enabled = bool(cur.fetchone()["enabled"])
                if not enabled:
                    return {"available": False, "reason": "pg_stat_statements extension is not enabled."}
                cur.execute(
                    """
                    SELECT
                        query,
                        calls,
                        total_exec_time,
                        mean_exec_time,
                        rows
                    FROM pg_stat_statements
                    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                    ORDER BY total_exec_time DESC
                    LIMIT 10
                    """
                )
                return {"available": True, "top_statements": [dict(row) for row in cur.fetchall()]}
        except Exception as exc:
            return {"available": False, "error": str(exc)}
