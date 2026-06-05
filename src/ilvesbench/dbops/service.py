from __future__ import annotations

from ilvesbench.config import PostgresConfig
from ilvesbench.db.postgres import PostgresInspector
from ilvesbench.dbops.catalog import DatabaseCatalog
from ilvesbench.dbops.migration import DataMigrationService
from ilvesbench.dbops.normalization import NormalizationWorkflow
from ilvesbench.dbops.profiler import DatabaseProfiler
from ilvesbench.dbops.target_db import TargetDatabaseManager
from ilvesbench.dbops.validation import QueryValidationService


class DBOpsService:
    """Database-access boundary for IlvesBench.

    DBOps owns operations that require PostgreSQL connectivity: metadata reads,
    database state checks, DDL/DML execution, validation, and metrics.
    """

    def __init__(self, config: PostgresConfig, postgres: PostgresInspector | None = None) -> None:
        self._config = config
        self.postgres = postgres or PostgresInspector(config)
        self._bind_subcomponents()

    def _bind_subcomponents(self) -> None:
        self.catalog = DatabaseCatalog(self.postgres)
        self.migration = DataMigrationService(self._config, self.postgres)
        self.profiler = DatabaseProfiler(self._config, self.postgres)
        self.normalization = NormalizationWorkflow()
        self.target_db = TargetDatabaseManager(self.postgres)
        self.validation = QueryValidationService(self._config, self.postgres)

    def replace_postgres(self, postgres: PostgresInspector) -> None:
        self.postgres = postgres
        self._bind_subcomponents()

    def check_connection_status(self) -> dict:
        return self.catalog.check_connection_status()

    def discover_databases(self) -> list[dict]:
        return self.catalog.discover_databases()

    def profile_databases(self) -> dict:
        return self.profiler.profile_databases()

    def validate_target_workload(self, statements: list[str]) -> list[dict]:
        return self.validation.validate_target_workload(statements)
