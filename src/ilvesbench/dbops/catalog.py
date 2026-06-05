from __future__ import annotations

from ilvesbench.db.postgres import PostgresInspector


class DatabaseCatalog:
    """Database discovery and schema metadata access."""

    def __init__(self, postgres: PostgresInspector) -> None:
        self._postgres = postgres

    def check_connection_status(self) -> dict:
        return self._postgres.check_connection_status()

    def discover_databases(self) -> list[dict]:
        return self._postgres.discover_databases()

    def database_exists(self, database: str) -> bool:
        return self._postgres.database_exists(database)

    def inspect_schema(self, database: str | None = None):
        return self._postgres.inspect_schema(database)


