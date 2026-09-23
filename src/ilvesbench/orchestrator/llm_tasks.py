from __future__ import annotations

from ilvesbench.benchmark.migration_planner import MigrationPlanner
from ilvesbench.benchmark.schema_transformer import SchemaTransformer
from ilvesbench.config import IlvesBenchConfig
from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import SchemaSnapshot


class LLMTaskService:
    """LLM-mediated planning tasks used by the workflow orchestrator."""

    def __init__(self, config: IlvesBenchConfig, llm: LLMGateway | None = None) -> None:
        self.schema_transformer = SchemaTransformer(
            llm=llm,
            target_database=config.postgres.new_database,
            chunking=config.schema_chunking,
        )
        self.migration_planner = MigrationPlanner(llm=llm)

    def plan_migration(self, schema: SchemaSnapshot | None, target_tables: list[dict]) -> dict:
        proposal = self.migration_planner.plan(schema, target_tables)
        return {
            "status": proposal.status,
            "summary": proposal.summary,
            "reasoning": proposal.rationale,
            "statements": proposal.statements,
            "target_tables": target_tables,
            "source": proposal.source,
            "llm_request": proposal.request_payload or {},
            "raw_response_text": proposal.raw_response_text,
        }

    def build_first_normal_form_decomposition(self, schema: SchemaSnapshot, approved_findings: list[dict]):
        return self.schema_transformer.build_first_normal_form_decomposition(schema, approved_findings)

    def discover_functional_dependencies(self, schema: SchemaSnapshot, target_tables: list[dict]):
        return self.schema_transformer.discover_functional_dependencies(schema, target_tables)

    def synthesize_third_normal_form(self, schema: SchemaSnapshot, target_tables: list[dict], approved_fds: list[dict]):
        return self.schema_transformer.synthesize_third_normal_form(schema, target_tables, approved_fds)

    def repair_schema(self, schema: SchemaSnapshot, target_tables: list[dict], sql_statements: list[str], error: str):
        return self.schema_transformer.repair(schema, target_tables, sql_statements, error)
