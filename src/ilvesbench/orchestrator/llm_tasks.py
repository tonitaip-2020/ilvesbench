from __future__ import annotations

from ilvesbench.benchmark.migration_planner import MigrationPlanner
from ilvesbench.benchmark.schema_transformer import SchemaTransformer
from ilvesbench.config import IlvesBenchConfig
from ilvesbench.llm.gateway import LLMGateway


class LLMTaskService:
    """LLM-mediated planning tasks used by the workflow orchestrator."""

    def __init__(self, config: IlvesBenchConfig, llm: LLMGateway | None = None) -> None:
        self.schema_transformer = SchemaTransformer(
            llm=llm,
            target_database=config.postgres.new_database,
        )
        self.migration_planner = MigrationPlanner(llm=llm)

