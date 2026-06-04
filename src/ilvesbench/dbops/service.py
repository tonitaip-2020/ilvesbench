from __future__ import annotations

from ilvesbench.config import PostgresConfig
from ilvesbench.db.postgres import PostgresInspector


class DBOpsService:
    """Database-access boundary for IlvesBench.

    DBOps owns operations that require PostgreSQL connectivity: metadata reads,
    database state checks, DDL/DML execution, validation, and metrics.
    """

    def __init__(self, config: PostgresConfig, postgres: PostgresInspector | None = None) -> None:
        self._config = config
        self.postgres = postgres or PostgresInspector(config)

    def check_connection_status(self) -> dict:
        return self.postgres.check_connection_status()

    def discover_databases(self) -> list[dict]:
        return self.postgres.discover_databases()

    def profile_databases(self) -> dict:
        return {
            "original": self.postgres.profile_database(self._config.original_database),
            "target": self.postgres.profile_database(self._config.new_database),
        }

