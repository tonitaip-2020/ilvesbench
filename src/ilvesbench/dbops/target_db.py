from __future__ import annotations

from ilvesbench.db.postgres import PostgresInspector


class TargetDatabaseManager:
    """Target database lifecycle and SQL execution operations."""

    def __init__(self, postgres: PostgresInspector) -> None:
        self._postgres = postgres

    def create_database_if_missing(self, database: str) -> str:
        return self._postgres.create_database_if_missing(database)

    def drop_database_if_exists(self, database: str) -> str:
        return self._postgres.drop_database_if_exists(database)

    def truncate_user_tables(self, database: str) -> list[str]:
        return self._postgres.truncate_user_tables(database)

    def execute_statements(self, database: str, statements: list[str]) -> list[dict]:
        return self._postgres.execute_statements(database, statements)

    def ensure_source_fdw(self, target_database: str, source_schema_alias: str, source_tables) -> list[str]:
        return self._postgres.ensure_source_fdw(target_database, source_schema_alias, source_tables)


