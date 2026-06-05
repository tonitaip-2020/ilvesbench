from __future__ import annotations

from ilvesbench.config import PostgresConfig
from ilvesbench.db.postgres import PostgresInspector


class QueryValidationService:
    """Target-side SQL validation and optional result comparison."""

    def __init__(self, config: PostgresConfig, postgres: PostgresInspector) -> None:
        self._config = config
        self._postgres = postgres

    def validate_workload_statements(self, database: str, statements: list[str]) -> list[dict]:
        return self._postgres.validate_workload_statements(database, statements)

    def validate_target_workload(self, statements: list[str]) -> list[dict]:
        try:
            target_exists = self._postgres.database_exists(self._config.new_database)
        except Exception:
            return []
        if not target_exists:
            return []
        try:
            return self.validate_workload_statements(self._config.new_database, statements)
        except Exception as exc:
            return [{"statement": "", "error": str(exc)}]

    def compare_query_results(self, source_statement: str, rewritten_statement: str) -> dict:
        return self._postgres.compare_query_results(
            self._config.original_database,
            self._config.new_database,
            source_statement,
            rewritten_statement,
        )


