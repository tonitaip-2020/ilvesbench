from __future__ import annotations

from ilvesbench.config import PostgresConfig
from ilvesbench.db.postgres import PostgresInspector


class DatabaseProfiler:
    """Database profile and metrics access."""

    def __init__(self, config: PostgresConfig, postgres: PostgresInspector) -> None:
        self._config = config
        self._postgres = postgres

    def profile_database(self, database: str) -> dict:
        return self._postgres.profile_database(database)

    def profile_databases(self) -> dict:
        return {
            "original": self.profile_database(self._config.original_database),
            "target": self.profile_database(self._config.new_database),
        }

    def collect_database_metrics(self, database: str) -> dict:
        return self._postgres.collect_database_metrics(database)


