from __future__ import annotations

from ilvesbench.config import PostgresConfig
from ilvesbench.db.postgres import PostgresInspector
from ilvesbench.models import SchemaSnapshot


class DataMigrationService:
    """DBOps data migration execution support."""

    def __init__(self, config: PostgresConfig, postgres: PostgresInspector) -> None:
        self._config = config
        self._postgres = postgres

    def source_tables_for_migration(self, schema: SchemaSnapshot, target_tables: list[dict]):
        source_table_names = {
            source_table
            for table in target_tables
            for source_table in table.get("source_tables", [])
        }
        return [
            table
            for table in schema.tables
            if f"{table.schema}.{table.name}" in source_table_names
        ]

    def execute_migration(
        self,
        *,
        run_id: str,
        schema: SchemaSnapshot,
        target_tables: list[dict],
        statements: list[str],
    ) -> dict:
        source_alias = f"ilvesbench_src_{run_id[-6:]}"
        referenced_tables = self.source_tables_for_migration(schema, target_tables)
        fdw_tables = self._postgres.ensure_source_fdw(
            self._config.new_database,
            source_alias,
            referenced_tables,
        )
        final_statements = [statement.replace("__SOURCE_SCHEMA__", f'"{source_alias}"') for statement in statements]
        executed = self._postgres.execute_statements(self._config.new_database, final_statements)
        return {
            "source_schema_alias": source_alias,
            "fdw_tables": fdw_tables,
            "executed_statement_count": len(executed),
            "executed_sql": final_statements,
        }

