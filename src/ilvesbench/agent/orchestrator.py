from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path, PureWindowsPath
import re
import traceback
import uuid

from ilvesbench.benchmark.energy import BenchmarkComparator, EnergyEstimator
from ilvesbench.benchmark.index_advisor import IndexAdvisor
from ilvesbench.benchmark.migration_planner import MigrationPlanner
from ilvesbench.benchmark.pgbench import PgBenchRunner
from ilvesbench.benchmark.schema_transformer import SchemaTransformer
from ilvesbench.benchmark.tuning import PostgresTuningAdvisor
from ilvesbench.benchmark.workload import WorkloadPlanner
from ilvesbench.config import IlvesBenchConfig
from ilvesbench.db.postgres import PostgresInspector
from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import (
    BenchmarkRunRecord,
    LogSummary,
    QueryObservation,
    SchemaSnapshot,
    StepResult,
    TransactionObservation,
    to_dict,
)
from ilvesbench.osops.hardware import HardwareInspector
from ilvesbench.osops.logs import PostgresLogParser
from ilvesbench.osops.workload_files import WorkloadFileParser
from ilvesbench.store.repository import RunRepository


class PipelineOrchestrator:
    def __init__(self, config: IlvesBenchConfig) -> None:
        self._config = config
        self._store = RunRepository(
            config.resolve_path(config.storage.sqlite_path),
            config.resolve_path(config.storage.artifact_dir),
        )
        self._llm = LLMGateway.from_config(config.llm)
        self._postgres = PostgresInspector(config.postgres)
        self._hardware = HardwareInspector(config.docker)
        self._logs = PostgresLogParser()
        self._workload_files = WorkloadFileParser()
        self._pgbench = PgBenchRunner()
        self._schema_transformer = SchemaTransformer(
            llm=self._llm,
            target_database=self._config.postgres.new_database,
        )
        self._migration_planner = MigrationPlanner(llm=self._llm)
        self._workload_planner = WorkloadPlanner(
            llm=self._llm,
            target_database=self._config.postgres.new_database,
        )
        self._index_advisor = IndexAdvisor()
        self._tuning_advisor = PostgresTuningAdvisor()
        self._energy_estimator = EnergyEstimator()
        self._benchmark_comparator = BenchmarkComparator()

    @property
    def store(self) -> RunRepository:
        return self._store

    def test_llm(self) -> dict:
        result = self._llm.generate(
            [{"role": "user", "content": "Say hello in one short sentence."}],
            max_tokens=50,
        )
        return to_dict(result)

    def check_postgres_connection(self) -> dict:
        return self._postgres.check_connection_status()

    def discover_postgres_databases(self) -> dict:
        return {
            "status": "ok",
            "original_database": self._config.postgres.original_database,
            "new_database": self._config.postgres.new_database,
            "schemas": self._config.postgres.schemas,
            "databases": self._postgres.discover_databases(),
        }

    def profile_databases(self) -> dict:
        return {
            "original": self._postgres.profile_database(self._config.postgres.original_database),
            "target": self._postgres.profile_database(self._config.postgres.new_database),
        }

    def load_run_record(self, run_id: str) -> BenchmarkRunRecord:
        record = self._store.get_run_record(run_id)
        if record is None:
            raise ValueError(f"Run not found: {run_id}")
        self._apply_run_database_context(record)
        return record

    def _apply_run_database_context(self, record: BenchmarkRunRecord) -> None:
        original_database = str(record.summary.get("original_database") or "").strip()
        new_database = str(record.summary.get("new_database") or "").strip()
        if not original_database and not new_database:
            return
        if original_database:
            self._config.postgres.original_database = original_database
        if new_database:
            self._config.postgres.new_database = new_database

    def create_mvp_record(self) -> BenchmarkRunRecord:
        now = datetime.now(UTC).isoformat()
        record = BenchmarkRunRecord(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            created_at=now,
            updated_at=now,
            status="running",
            config_path=self._config.config_path,
            steps=self._pipeline_template(),
            summary={
                "mode": "mvp_collection",
                "original_database": self._config.postgres.original_database,
                "new_database": self._config.postgres.new_database,
                "schemas": list(self._config.postgres.schemas),
            },
        )
        self._store.upsert_run(record)
        return record

    def execute_mvp_collection(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        try:
            record = self._run_llm_step(record)
            schema = self._run_schema_step(record)
            first_normal_form_scan = self._run_first_normal_form_step(record, schema)
            record = self._run_hardware_step(record)
            log_summary = self._run_logs_step(record)
            record = self._plan_pgbench_original_step(record)

            if schema is not None:
                first_normal_form_findings = (
                    first_normal_form_scan.get("findings", [])
                    if first_normal_form_scan is not None
                    else []
                )
                proposal = self._schema_transformer.analyze(schema, first_normal_form_findings)
                proposal_dict = {
                    "status": proposal.status,
                    "summary": proposal.summary,
                    "rationale": proposal.rationale,
                    "table_findings": proposal.table_findings,
                    "functional_dependencies": proposal.functional_dependencies,
                    "first_normal_form_findings": first_normal_form_findings,
                    "target_tables": proposal.target_tables,
                    "sql_statements": proposal.sql_statements,
                    "source": proposal.source,
                }
                review_candidates = self._normalization_review_candidates(
                    schema,
                    first_normal_form_findings,
                    proposal.target_tables,
                )
                if review_candidates:
                    proposal_dict.update(
                        {
                            "status": "awaiting_review",
                            "summary": (
                                f"{len(review_candidates)} suspicious table(s) need human normalization review "
                                "before final DDL is generated."
                            ),
                            "review_status": "pending",
                            "normalization_reviews": review_candidates,
                            "draft_target_tables": proposal.target_tables,
                            "draft_sql_statements": proposal.sql_statements,
                            "target_tables": [],
                            "sql_statements": [],
                        }
                    )
                record.artifacts["propose_3nf_schema"] = self._store.write_artifact(
                    record.run_id,
                    "normalization_proposal",
                    proposal_dict | {"raw_response_text": proposal.raw_response_text},
                )
                if review_candidates:
                    record = self._input_required_step(record, "propose_3nf_schema", proposal_dict)
                else:
                    record = self._complete_step(
                        record,
                        "propose_3nf_schema",
                        proposal_dict,
                    )
            else:
                record = self._fail_step(
                    record,
                    "propose_3nf_schema",
                    "3NF planning is blocked until schema inspection succeeds.",
                )

            workload_plan = self._workload_planner.build_plan(log_summary)
            record = self._mark_planned(
                record,
                "suggest_summary_tables",
                {
                    "status": workload_plan.status,
                    "summary": workload_plan.summary,
                    "candidate_count": len(workload_plan.candidate_summary_tables),
                    "candidates": workload_plan.candidate_summary_tables,
                    "creation_status": "placeholder",
                },
            )

            record = self._plan_target_schema_step(record)
            record = self._plan_migration_step(record, schema)
            record = self._plan_rewrite_queries_step(record, log_summary)
            record = self._plan_index_recommendations_step(record, schema, log_summary)
            record = self._run_tuning_step(record, log_summary)
            record = self._plan_pgbench_new_step(record)
            record = self._run_extended_metrics_step(record, [self._config.postgres.original_database])
            record = self._update_benchmark_comparison_step(record)

            record.summary.update(
                self._summary_counts(record)
            )
            record.status = self._overall_status(record)
        except Exception as exc:  # pragma: no cover - exercised in runtime integration
            record.status = "failed"
            record.summary["error"] = str(exc)
            record.summary["traceback"] = traceback.format_exc()
        finally:
            record.updated_at = datetime.now(UTC).isoformat()
            self._store.upsert_run(record)
        return record

    def run_mvp_collection(self) -> BenchmarkRunRecord:
        return self.execute_mvp_collection(self.create_mvp_record())

    def begin_create_target_schema(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        record = self._replace_step(
            record,
            "create_target_schema",
            status="running",
            details=next((step.details for step in record.steps if step.name == "create_target_schema"), {}),
            error=None,
            planned_only=False,
        )
        record.summary.update(self._summary_counts(record))
        self._store.upsert_run(record)
        return record

    def execute_create_target_schema(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        step = next(step for step in record.steps if step.name == "create_target_schema")
        try:
            sql_statements = list(step.details.get("sql_statements", []))
            if not sql_statements:
                raise ValueError("No schema-creation SQL is available for this run.")

            database_result = self._postgres.create_database_if_missing(self._config.postgres.new_database)
            executed = self._postgres.execute_statements(self._config.postgres.new_database, sql_statements)
            record = self._complete_step(
                record,
                "create_target_schema",
                {
                    **step.details,
                    "database_name": self._config.postgres.new_database,
                    "database_result": database_result,
                    "executed_statement_count": len(executed),
                    "summary": (
                        f'Created or reused database "{self._config.postgres.new_database}" and applied '
                        f"{len(executed)} target-schema statements."
                    ),
                },
            )
            record.summary["target_schema_created"] = True
            record.summary["target_database_result"] = database_result
            record.summary.update(self._summary_counts(record))
            record.status = self._overall_status(record)
        except Exception as exc:
            record = self._replace_step(
                record,
                "create_target_schema",
                status="failed",
                details={
                    **step.details,
                    "summary": (
                        "Schema creation failed. Review the PostgreSQL error, then repair the DDL "
                        f"with the LLM or reset {self._config.postgres.new_database} and try again."
                    ),
                },
                error=str(exc),
                planned_only=False,
            )
            record.status = self._overall_status(record)
            record.summary["error"] = str(exc)
        finally:
            record.updated_at = datetime.now(UTC).isoformat()
            self._store.upsert_run(record)
        return record

    def begin_migrate_data(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        record = self._replace_step(
            record,
            "migrate_data",
            status="running",
            details=next((step.details for step in record.steps if step.name == "migrate_data"), {}),
            error=None,
            planned_only=False,
        )
        record.summary.update(self._summary_counts(record))
        self._store.upsert_run(record)
        return record

    def execute_migrate_data(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        try:
            create_step = next(step for step in record.steps if step.name == "create_target_schema")
            if create_step.status != "completed":
                raise ValueError(f"Create {self._config.postgres.new_database} schema must be completed before data migration.")

            migrate_step = next(step for step in record.steps if step.name == "migrate_data")
            schema = self._load_schema_artifact(record)
            if not migrate_step.details.get("statements"):
                record = self._plan_migration_step(record, schema)
                migrate_step = next(step for step in record.steps if step.name == "migrate_data")
            if migrate_step.details.get("source") == "deterministic_1nf":
                refreshed = self._migration_planner.plan(schema, migrate_step.details.get("target_tables", []))
                if refreshed.statements:
                    migration_sql = "\n\n".join(statement["sql"] for statement in refreshed.statements) + "\n"
                    migration_path = self._store.write_text_artifact(
                        record.run_id,
                        "migration_plan",
                        migration_sql,
                        suffix=".sql",
                    )
                    migrate_step.details.update(
                        {
                            "status": refreshed.status,
                            "summary": refreshed.summary,
                            "reasoning": refreshed.rationale,
                            "statements": refreshed.statements,
                            "source": refreshed.source,
                            "sql_path": migration_path,
                        }
                    )
                    record.artifacts["migration_plan_sql"] = migration_path
                    record.artifacts["migrate_data"] = self._store.write_artifact(
                        record.run_id,
                        "migration_plan",
                        migrate_step.details,
                    )
            statements = [statement["sql"] for statement in migrate_step.details.get("statements", [])]
            if not statements:
                raise ValueError("No migration SQL is available for this run.")

            source_alias = f"ilvesbench_src_{record.run_id[-6:]}"
            referenced_tables = self._source_tables_for_migration(schema, migrate_step.details.get("target_tables", []))
            fdw_tables = self._postgres.ensure_source_fdw(
                self._config.postgres.new_database,
                source_alias,
                referenced_tables,
            )
            final_statements = [statement.replace("__SOURCE_SCHEMA__", f'"{source_alias}"') for statement in statements]
            executed = self._postgres.execute_statements(self._config.postgres.new_database, final_statements)
            record = self._complete_step(
                record,
                "migrate_data",
                {
                    **migrate_step.details,
                    "source_schema_alias": source_alias,
                    "fdw_tables": fdw_tables,
                    "executed_statement_count": len(executed),
                    "executed_sql": final_statements,
                    "summary": f"Executed {len(executed)} migration statements into {self._config.postgres.new_database}.",
                },
            )
            record.summary["migration_completed"] = True
            record = self._plan_pgbench_new_step(record)
            record = self._run_extended_metrics_step(
                record,
                [self._config.postgres.original_database, self._config.postgres.new_database],
            )
            record = self._update_benchmark_comparison_step(record)
            record.summary.update(self._summary_counts(record))
            record.status = self._overall_status(record)
        except Exception as exc:
            record = self._fail_step(record, "migrate_data", str(exc))
            record.status = "failed"
            record.summary["error"] = str(exc)
        finally:
            record.updated_at = datetime.now(UTC).isoformat()
            self._store.upsert_run(record)
        return record

    def begin_regenerate_rewrite_queries(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        rewrite_step = next((step for step in record.steps if step.name == "rewrite_queries"), None)
        if rewrite_step is not None:
            record = self._replace_step(
                record,
                "rewrite_queries",
                status="running",
                details=rewrite_step.details,
                error=None,
                planned_only=False,
            )
        record.summary.update(self._summary_counts(record))
        self._store.upsert_run(record)
        return record

    def execute_regenerate_rewrite_queries(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        try:
            log_summary = self._load_log_summary_artifact(record)
            schema = self._load_schema_artifact(record)
            record = self._plan_rewrite_queries_step(record, log_summary)
            record = self._plan_index_recommendations_step(record, schema, log_summary)
            record = self._plan_pgbench_new_step(record)
            record.summary.pop("error", None)
        except Exception as exc:
            record = self._replace_step(
                record,
                "rewrite_queries",
                status="failed",
                details=next((step.details for step in record.steps if step.name == "rewrite_queries"), {}),
                error=str(exc),
                planned_only=False,
            )
            record.summary["error"] = str(exc)
        record.summary.update(self._summary_counts(record))
        record.status = self._overall_status(record)
        record.updated_at = datetime.now(UTC).isoformat()
        self._store.upsert_run(record)
        return record

    def begin_reset_target_db(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        record.summary.update(self._summary_counts(record))
        self._store.upsert_run(record)
        return record

    def execute_reset_target_db(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        try:
            result = self._postgres.drop_database_if_exists(self._config.postgres.new_database)
            create_step = next(step for step in record.steps if step.name == "create_target_schema")
            record = self._replace_step(
                record,
                "create_target_schema",
                status="planned",
                details={**create_step.details, "database_result": result, "summary": "Target database reset. Approval-gated SQL is ready to run again."},
                error=None,
                planned_only=True,
            )
            migrate_step = next(step for step in record.steps if step.name == "migrate_data")
            migrate_status = "planned" if migrate_step.details.get("statements") else migrate_step.status
            record = self._replace_step(
                record,
                "migrate_data",
                status=migrate_status,
                details=migrate_step.details,
                error=None,
                planned_only=migrate_status == "planned",
            )
            record.summary.pop("error", None)
            record.summary["target_schema_created"] = False
            record.summary["migration_completed"] = False
            record.summary["target_database_result"] = result
            run_pgbench_new_step = next(step for step in record.steps if step.name == "run_pgbench_new")
            record = self._replace_step(
                record,
                "run_pgbench_new",
                status="planned",
                details={
                    **run_pgbench_new_step.details,
                    "summary": (
                        f"{self._config.postgres.new_database} was reset. Recreate the schema, rerun migration, "
                        f"and then start the {self._config.postgres.new_database} benchmark when ready."
                    ),
                },
                error=None,
                planned_only=True,
            )
            record.summary.update(self._summary_counts(record))
            record.status = self._overall_status(record)
        except Exception as exc:
            record.status = "failed"
            record.summary["error"] = str(exc)
        finally:
            record.updated_at = datetime.now(UTC).isoformat()
            self._store.upsert_run(record)
        return record

    def begin_truncate_target_data(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        record.summary.update(self._summary_counts(record))
        self._store.upsert_run(record)
        return record

    def execute_truncate_target_data(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        try:
            if not self._postgres.database_exists(self._config.postgres.new_database):
                raise ValueError(f"{self._config.postgres.new_database} does not exist yet.")

            truncated_tables = self._postgres.truncate_user_tables(self._config.postgres.new_database)
            migrate_step = next(step for step in record.steps if step.name == "migrate_data")
            migrate_status = "planned" if migrate_step.details.get("statements") else migrate_step.status
            record = self._replace_step(
                record,
                "migrate_data",
                status=migrate_status,
                details={
                    **migrate_step.details,
                    "truncated_table_count": len(truncated_tables),
                    "truncated_tables": truncated_tables[:80],
                    "summary": (
                        f"Truncated {len(truncated_tables)} table(s) in {self._config.postgres.new_database}. "
                        "Structure remains; data migration can be rerun."
                    ),
                },
                error=None,
                planned_only=migrate_status == "planned",
            )
            run_pgbench_new_step = next(step for step in record.steps if step.name == "run_pgbench_new")
            record = self._replace_step(
                record,
                "run_pgbench_new",
                status="planned",
                details={
                    **run_pgbench_new_step.details,
                    "summary": (
                        f"{self._config.postgres.new_database} data was truncated. Rerun migration before benchmarking."
                    ),
                },
                error=None,
                planned_only=True,
            )
            record.summary.pop("error", None)
            record.summary["target_schema_created"] = True
            record.summary["migration_completed"] = False
            record.summary["target_data_truncated"] = True
            record.summary.update(self._summary_counts(record))
            record.status = self._overall_status(record)
        except Exception as exc:
            record.status = "failed"
            record.summary["error"] = str(exc)
        finally:
            record.updated_at = datetime.now(UTC).isoformat()
            self._store.upsert_run(record)
        return record

    def apply_normalization_review(self, run_id: str, candidate_id: str, decision: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        if decision not in {"approved", "rejected"}:
            raise ValueError("Normalization review decision must be approved or rejected.")

        schema = self._load_schema_artifact(record)
        normalization_step = next(step for step in record.steps if step.name == "propose_3nf_schema")
        details = dict(normalization_step.details)
        reviews = [dict(item) for item in details.get("normalization_reviews", [])]
        if not reviews:
            raise ValueError("This run has no pending normalization reviews.")

        matched = False
        for review in reviews:
            if str(review.get("id")) == candidate_id:
                review["status"] = decision
                matched = True
                break
        if not matched:
            raise ValueError(f"Normalization review candidate not found: {candidate_id}")

        details["normalization_reviews"] = reviews
        pending = [review for review in reviews if review.get("status") == "pending"]
        if pending:
            details["review_status"] = "pending"
            details["summary"] = (
                f"{len(pending)} suspicious table(s) still need approve/reject decisions before final DDL is generated."
            )
            record = self._input_required_step(record, "propose_3nf_schema", details)
            record.summary.update(self._summary_counts(record))
            record.status = self._overall_status(record)
            self._store.upsert_run(record)
            return record

        approved_findings = [
            finding
            for review in reviews
            if review.get("status") == "approved"
            for finding in review.get("findings", [])
        ]
        if approved_findings:
            proposal = self._schema_transformer.build_first_normal_form_decomposition(schema, approved_findings)
            if proposal is None:
                raise ValueError("Approved normalization reviews did not produce a target-table proposal.")
            details.update(
                {
                    "status": proposal.status,
                    "summary": (
                        f"Generated final DDL from {len(approved_findings)} approved normalization finding(s)."
                    ),
                    "rationale": proposal.rationale,
                    "table_findings": proposal.table_findings,
                    "functional_dependencies": proposal.functional_dependencies,
                    "target_tables": proposal.target_tables,
                    "sql_statements": proposal.sql_statements,
                    "source": "human_reviewed_1nf",
                    "review_status": "completed",
                }
            )
        else:
            details.update(
                {
                    "status": "appears_3nf",
                    "summary": "All normalization candidates were rejected; no target DDL was generated.",
                    "rationale": ["Human review rejected all suspicious-table normalization candidates."],
                    "table_findings": [],
                    "functional_dependencies": [],
                    "target_tables": [],
                    "sql_statements": [],
                    "source": "human_review",
                    "review_status": "completed",
                }
            )

        record.artifacts["propose_3nf_schema"] = self._store.write_artifact(
            record.run_id,
            "normalization_proposal",
            details,
        )
        record = self._complete_step(record, "propose_3nf_schema", details)
        record = self._plan_target_schema_step(record)
        record = self._plan_migration_step(record, schema)
        log_summary = self._load_log_summary_artifact(record)
        record = self._plan_rewrite_queries_step(record, log_summary)
        record = self._plan_index_recommendations_step(record, schema, log_summary)
        record = self._plan_pgbench_new_step(record)
        record.summary.pop("error", None)
        record.summary.update(self._summary_counts(record))
        record.status = self._overall_status(record)
        record.updated_at = datetime.now(UTC).isoformat()
        self._store.upsert_run(record)
        return record

    def begin_repair_target_schema(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        create_step = next((step for step in record.steps if step.name == "create_target_schema"), None)
        if create_step is not None:
            record = self._replace_step(
                record,
                "create_target_schema",
                status="running",
                details=create_step.details,
                error=None,
                planned_only=False,
            )
        self._store.upsert_run(record)
        return record

    def execute_repair_target_schema(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        try:
            schema = self._load_schema_artifact(record)
            normalization_step = next(step for step in record.steps if step.name == "propose_3nf_schema")
            create_step = next(step for step in record.steps if step.name == "create_target_schema")
            repaired = self._schema_transformer.repair(
                schema,
                normalization_step.details.get("target_tables", []),
                normalization_step.details.get("sql_statements", []),
                create_step.error or record.summary.get("error", ""),
            )
            proposal_dict = {
                "status": repaired.status,
                "summary": repaired.summary,
                "rationale": repaired.rationale,
                "table_findings": repaired.table_findings,
                "functional_dependencies": repaired.functional_dependencies or normalization_step.details.get("functional_dependencies", []),
                "target_tables": repaired.target_tables,
                "sql_statements": repaired.sql_statements,
                "source": repaired.source,
            }
            record.artifacts["propose_3nf_schema"] = self._store.write_artifact(
                record.run_id,
                "normalization_proposal",
                proposal_dict | {"raw_response_text": repaired.raw_response_text},
            )
            record = self._complete_step(record, "propose_3nf_schema", proposal_dict)
            record = self._plan_target_schema_step(record)
            record = self._plan_migration_step(record, schema)
            record = self._plan_rewrite_queries_step(record, None)
            record = self._plan_index_recommendations_step(record, schema, None)
            record = self._plan_pgbench_new_step(record)
            record.summary.pop("error", None)
            record.summary.update(self._summary_counts(record))
            record.status = self._overall_status(record)
        except Exception as exc:
            record = self._replace_step(
                record,
                "create_target_schema",
                status="failed",
                details={
                    **create_step.details,
                    "summary": (
                        "Schema repair failed. Review the error and either repair again or reset "
                        f"{self._config.postgres.new_database}."
                    ),
                },
                error=str(exc),
                planned_only=False,
            )
            record.status = self._overall_status(record)
            record.summary["error"] = str(exc)
        finally:
            record.updated_at = datetime.now(UTC).isoformat()
            self._store.upsert_run(record)
        return record

    def _run_llm_step(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        try:
            llm_result = self.test_llm()
            record = self._complete_step(record, "llm_gateway", {"response": llm_result["response_text"]})
            record.artifacts["llm_gateway"] = self._store.write_artifact(record.run_id, "llm_gateway", llm_result)
        except Exception as exc:
            record = self._fail_step(record, "llm_gateway", str(exc))
        return record

    def _run_schema_step(self, record: BenchmarkRunRecord):
        try:
            schema = self._postgres.inspect_schema()
            schema_dict = to_dict(schema)
            record.artifacts["inspect_source_schema"] = self._store.write_artifact(
                record.run_id, "schema_snapshot", schema_dict
            )
            self._complete_step(
                record,
                "inspect_source_schema",
                {
                    "database": schema.database,
                    "table_count": len(schema.tables),
                    "database_size_bytes": schema.database_size_bytes,
                },
            )
            return schema
        except Exception as exc:
            self._fail_step(record, "inspect_source_schema", str(exc))
            return None

    def _run_first_normal_form_step(self, record: BenchmarkRunRecord, schema: SchemaSnapshot | None) -> dict | None:
        if schema is None:
            self._fail_step(record, "scan_first_normal_form", "1NF scanning is blocked until schema inspection succeeds.")
            return None
        try:
            scan = self._postgres.scan_first_normal_form_warnings(schema)
            record.artifacts["scan_first_normal_form"] = self._store.write_artifact(
                record.run_id,
                "first_normal_form_scan",
                scan,
            )
            self._complete_step(
                record,
                "scan_first_normal_form",
                {
                    "status": scan.get("status"),
                    "summary": scan.get("summary"),
                    "finding_count": scan.get("finding_count", 0),
                    "findings": scan.get("findings", [])[:12],
                },
            )
            return scan
        except Exception as exc:
            self._replace_step(
                record,
                "scan_first_normal_form",
                status="completed",
                details={
                    "status": "unavailable",
                    "summary": f"1NF warning scan could not run: {exc}",
                    "finding_count": 0,
                    "findings": [],
                },
                error=None,
                planned_only=False,
            )
            return None

    def _run_hardware_step(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        try:
            hardware = self._hardware.collect(self._config.resolve_path("."))
            hardware_dict = to_dict(hardware)
            record = self._complete_step(
                record,
                "capture_hardware",
                {
                    "scope": hardware.scope,
                    "platform": hardware.platform,
                    "container_name": hardware.container_name,
                    "cpu_count": hardware.cpu_count,
                    "memory_total_bytes": hardware.memory_total_bytes,
                    "disk_total_bytes": hardware.disk_total_bytes,
                },
            )
            record.artifacts["capture_hardware"] = self._store.write_artifact(
                record.run_id, "hardware_snapshot", hardware_dict
            )
        except Exception as exc:
            record = self._fail_step(record, "capture_hardware", str(exc))
        return record

    def _run_logs_step(self, record: BenchmarkRunRecord):
        log_path = self._config.resolve_path(self._config.logs.path)
        resolved_workload_path = self._resolve_workload_path()

        try:
            if log_path.exists():
                log_summary = self._logs.parse(log_path, max_lines=self._config.logs.max_lines)
            elif resolved_workload_path is not None and resolved_workload_path.exists():
                log_summary = self._workload_files.parse(resolved_workload_path)
            else:
                self._input_required_step(
                    record,
                    "extract_workload_logs",
                    {
                        "summary": "No PostgreSQL log file or workload SQL file was found.",
                        "recommended_action": "Provide a workload file path in the UI and start a new run.",
                        "checked_log_path": str(log_path),
                        "checked_workload_path": str(resolved_workload_path) if resolved_workload_path else "",
                    },
                )
                return None

            log_dict = to_dict(log_summary)
            record.artifacts["extract_workload_logs"] = self._store.write_artifact(
                record.run_id, "log_summary", log_dict
            )
            self._complete_step(
                record,
                "extract_workload_logs",
                {
                    "source_kind": log_summary.source_kind,
                    "source_path": log_summary.path,
                    "statements_detected": log_summary.statements_detected,
                    "transactions_detected": log_summary.transactions_detected,
                    "top_query_count": len(log_summary.top_queries),
                },
            )
            return log_summary
        except Exception as exc:
            self._fail_step(record, "extract_workload_logs", str(exc))
            return None

    def _run_pgbench_original_step(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        step = next(step for step in record.steps if step.name == "run_pgbench_original")
        workload_path = self._resolve_workload_path()
        try:
            benchmark = self._pgbench.run(
                self._config.pgbench,
                self._config.postgres,
                self._config.postgres.original_database,
                workload_path=workload_path,
            )
            record = self._store_pgbench_result(
                record,
                "run_pgbench_original",
                artifact_name="pgbench_original",
                benchmark=benchmark,
                workload_path=workload_path,
            )
        except Exception as exc:
            record = self._replace_step(
                record,
                "run_pgbench_original",
                status="failed",
                details=step.details,
                error=str(exc),
                planned_only=False,
            )
        return record

    def _run_pgbench_new_step(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        step = next(step for step in record.steps if step.name == "run_pgbench_new")
        workload_path = self._rewritten_workload_path(record)
        try:
            benchmark = self._pgbench.run(
                self._config.pgbench,
                self._config.postgres,
                self._config.postgres.new_database,
                workload_path=workload_path,
            )
            record = self._store_pgbench_result(
                record,
                "run_pgbench_new",
                artifact_name="pgbench_new",
                benchmark=benchmark,
                workload_path=workload_path,
            )
        except Exception as exc:
            record = self._replace_step(
                record,
                "run_pgbench_new",
                status="failed",
                details=step.details,
                error=str(exc),
                planned_only=False,
            )
        return record

    def begin_pgbench_original(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        step = next((step for step in record.steps if step.name == "run_pgbench_original"), None)
        if step is not None:
            record = self._replace_step(
                record,
                "run_pgbench_original",
                status="running",
                details=step.details,
                error=None,
                planned_only=False,
            )
        record.summary.update(self._summary_counts(record))
        self._store.upsert_run(record)
        return record

    def execute_pgbench_original(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        try:
            record = self._run_pgbench_original_step(record)
            record = self._update_benchmark_comparison_step(record)
        except Exception as exc:
            record = self._replace_step(
                record,
                "run_pgbench_original",
                status="failed",
                details=next((step.details for step in record.steps if step.name == "run_pgbench_original"), {}),
                error=str(exc),
                planned_only=False,
            )
        record.summary.update(self._summary_counts(record))
        record.status = self._overall_status(record)
        record.updated_at = datetime.now(UTC).isoformat()
        self._store.upsert_run(record)
        return record

    def begin_pgbench_new(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        step = next((step for step in record.steps if step.name == "run_pgbench_new"), None)
        if step is not None:
            record = self._replace_step(
                record,
                "run_pgbench_new",
                status="running",
                details=step.details,
                error=None,
                planned_only=False,
            )
        record.summary.update(self._summary_counts(record))
        self._store.upsert_run(record)
        return record

    def execute_pgbench_new(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        try:
            create_step = next(step for step in record.steps if step.name == "create_target_schema")
            migrate_step = next(step for step in record.steps if step.name == "migrate_data")
            rewrite_step = next(step for step in record.steps if step.name == "rewrite_queries")
            target_exists = self._postgres.database_exists(self._config.postgres.new_database)
            target_has_data = self._target_database_has_data()
            if create_step.status != "completed" and not target_exists:
                raise ValueError(
                    f"Create {self._config.postgres.new_database} schema or provide an existing "
                    f"{self._config.postgres.new_database} database before benchmarking it."
                )
            if migrate_step.status != "completed" and not target_has_data:
                raise ValueError(
                    f"Populate {self._config.postgres.new_database} or provide an existing populated "
                    f"{self._config.postgres.new_database} database before benchmarking it."
                )
            if rewrite_step.status != "completed" or not rewrite_step.details.get("workload_path"):
                raise ValueError(f"Rewritten {self._config.postgres.new_database} workload must be completed before benchmarking.")
            workload_path = Path(str(rewrite_step.details.get("workload_path"))).resolve()
            if workload_path.exists():
                validation_errors = self._validate_db_new_workload(
                    self._split_sql_statements(workload_path.read_text(encoding="utf-8", errors="replace"))
                )
                if validation_errors:
                    sample_errors = " | ".join(
                        str(item.get("error", "")).splitlines()[0]
                        for item in validation_errors[:3]
                    )
                    raise ValueError(
                        f"The current {self._config.postgres.new_database} workload does not validate against "
                        f"{self._config.postgres.new_database}. "
                        f"Regenerate it before benchmarking. {sample_errors}"
                    )
            record = self._run_pgbench_new_step(record)
            record = self._run_extended_metrics_step(
                record,
                [self._config.postgres.original_database, self._config.postgres.new_database],
            )
            record = self._update_benchmark_comparison_step(record)
        except Exception as exc:
            record = self._replace_step(
                record,
                "run_pgbench_new",
                status="failed",
                details=next((step.details for step in record.steps if step.name == "run_pgbench_new"), {}),
                error=str(exc),
                planned_only=False,
            )
        record.summary.update(self._summary_counts(record))
        record.status = self._overall_status(record)
        record.updated_at = datetime.now(UTC).isoformat()
        self._store.upsert_run(record)
        return record

    def begin_create_secondary_indexes(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        record.status = "running"
        step = self._secondary_index_step(record)
        record = self._replace_step(
            record,
            "create_secondary_indexes",
            status="running",
            details=step.details,
            error=None,
            planned_only=False,
        )
        record.summary.update(self._summary_counts(record))
        self._store.upsert_run(record)
        return record

    def execute_create_secondary_indexes(self, run_id: str) -> BenchmarkRunRecord:
        record = self.load_run_record(run_id)
        step = self._secondary_index_step(record)
        try:
            create_step = next(step for step in record.steps if step.name == "create_target_schema")
            target_exists = self._postgres.database_exists(self._config.postgres.new_database)
            if create_step.status != "completed" and not target_exists:
                raise ValueError(
                    f"Create {self._config.postgres.new_database} schema or provide an existing "
                    f"{self._config.postgres.new_database} database before secondary indexes can be created."
                )

            statements = self._secondary_index_sql_statements(record)
            if not statements:
                raise ValueError("No secondary-index SQL statements are available for this run.")

            executed = self._postgres.execute_statements(self._config.postgres.new_database, statements)
            record = self._complete_step(
                record,
                "create_secondary_indexes",
                {
                    **step.details,
                    "executed_statement_count": len(executed),
                    "summary": f"Created or reused {len(executed)} secondary index(es) on {self._config.postgres.new_database}.",
                },
            )
            record = self._run_extended_metrics_step(
                record,
                [self._config.postgres.original_database, self._config.postgres.new_database],
            )
        except Exception as exc:
            record = self._replace_step(
                record,
                "create_secondary_indexes",
                status="failed",
                details=step.details,
                error=str(exc),
                planned_only=False,
            )
            record.summary["error"] = str(exc)
        record.summary.update(self._summary_counts(record))
        record.status = self._overall_status(record)
        record.updated_at = datetime.now(UTC).isoformat()
        self._store.upsert_run(record)
        return record

    def _secondary_index_step(self, record: BenchmarkRunRecord) -> StepResult:
        step = next((step for step in record.steps if step.name == "create_secondary_indexes"), None)
        if step is not None:
            return step
        details = {
            "summary": f"Approval-gated: create recommended secondary indexes on {self._config.postgres.new_database}.",
            "recommendation_count": len(self._secondary_index_sql_statements(record)),
            "sql_statements": self._secondary_index_sql_statements(record),
        }
        record.steps.append(
            StepResult(
                name="create_secondary_indexes",
                title="Create secondary indexes",
                status="planned" if details["sql_statements"] else "pending",
                requires_approval=True,
                planned_only=True,
                details=details,
            )
        )
        return record.steps[-1]

    def _secondary_index_sql_statements(self, record: BenchmarkRunRecord) -> list[str]:
        statements: list[str] = []
        for step_name in ("create_secondary_indexes", "optimize_indexes"):
            step = next((step for step in record.steps if step.name == step_name), None)
            if step is None:
                continue
            for statement in step.details.get("sql_statements", []):
                if statement and statement not in statements:
                    statements.append(statement)
        return statements

    def _target_database_has_data(self) -> bool:
        try:
            profile = self._postgres.profile_database(self._config.postgres.new_database)
        except Exception:
            return False
        counts = profile.get("counts", {})
        return int(counts.get("estimated_rows") or 0) > 0 or int(counts.get("populated_tables") or 0) > 0

    def _store_pgbench_result(
        self,
        record: BenchmarkRunRecord,
        step_name: str,
        *,
        artifact_name: str,
        benchmark,
        workload_path: Path | None,
    ) -> BenchmarkRunRecord:
        benchmark_dict = to_dict(benchmark)
        record.artifacts[step_name] = self._store.write_artifact(
            record.run_id,
            artifact_name,
            benchmark_dict,
        )
        energy_dict = self._store_energy_estimate(record, step_name, benchmark)
        details = {
            "benchmark_status": benchmark.status,
            "database": benchmark.database,
            "duration_seconds": benchmark.duration_seconds,
            "clients": benchmark.clients,
            "jobs": benchmark.jobs,
            "workload_path": str(workload_path) if workload_path is not None else "",
            "throughput_tps": benchmark.throughput_tps,
            "average_latency_ms": benchmark.average_latency_ms,
            "energy_status": energy_dict.get("status"),
            "energy_joules": energy_dict.get("energy_joules"),
            "joules_per_transaction": energy_dict.get("joules_per_transaction"),
        }
        if benchmark.status == "completed":
            details["summary"] = (
                f'pgbench completed against "{benchmark.database}" with '
                f"{benchmark.throughput_tps} tps and {benchmark.average_latency_ms} ms average latency."
            )
            return self._complete_step(record, step_name, details)

        if benchmark.status == "skipped":
            details["summary"] = self._pgbench_error_summary(benchmark.stderr or benchmark.stdout) or "pgbench was skipped."
            return self._complete_step(record, step_name, details)

        fatal_error = self._pgbench_error_summary(benchmark.stderr or benchmark.stdout)
        details["fatal_error"] = fatal_error
        details["summary"] = fatal_error or "pgbench did not run."
        return self._replace_step(
            record,
            step_name,
            status="failed",
            details=details,
            error=fatal_error or "pgbench did not run.",
            planned_only=False,
        )

    def _store_energy_estimate(self, record: BenchmarkRunRecord, step_name: str, benchmark) -> dict:
        hardware = self._load_optional_artifact(record, "capture_hardware") or {}
        energy = self._energy_estimator.estimate(benchmark, hardware, self._config.energy)
        energy_dict = to_dict(energy)
        record.artifacts[f"{step_name}_energy"] = self._store.write_artifact(
            record.run_id,
            f"{step_name}_energy",
            energy_dict,
        )
        return energy_dict

    def _update_benchmark_comparison_step(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        original = self._load_optional_artifact(record, "run_pgbench_original")
        normalized = self._load_optional_artifact(record, "run_pgbench_new")
        original_energy = self._load_optional_artifact(record, "run_pgbench_original_energy")
        normalized_energy = self._load_optional_artifact(record, "run_pgbench_new_energy")
        comparison = self._benchmark_comparator.compare(
            original=original,
            normalized=normalized,
            original_energy=original_energy,
            normalized_energy=normalized_energy,
            storage=self._storage_comparison(record),
        )
        record.artifacts["compare_disk_usage"] = self._store.write_artifact(
            record.run_id,
            "benchmark_comparison",
            comparison,
        )
        if comparison.get("status") == "completed":
            return self._complete_step(record, "compare_disk_usage", comparison)
        return self._mark_planned(record, "compare_disk_usage", comparison)

    def _normalization_review_candidates(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict],
        draft_target_tables: list[dict],
    ) -> list[dict]:
        reviewable_findings = [
            finding
            for finding in first_normal_form_findings
            if finding.get("pattern") == "delimited_multi_value_column"
            and str(finding.get("table", "")).strip()
            and str(finding.get("column", "")).strip()
        ]
        if not reviewable_findings:
            return []

        source_tables = {f"{table.schema}.{table.name}": table for table in schema.tables}
        findings_by_table: dict[str, list[dict]] = {}
        for finding in reviewable_findings:
            table_name = str(finding.get("table", "")).strip()
            findings_by_table.setdefault(table_name, []).append(finding)

        candidates: list[dict] = []
        for table_name in sorted(findings_by_table):
            findings = findings_by_table[table_name]
            source_table = source_tables.get(table_name)
            columns = [str(finding.get("column", "")) for finding in findings if str(finding.get("column", ""))]
            relevant_targets = [
                table
                for table in draft_target_tables
                if table_name in table.get("source_tables", [])
            ]
            reasons = [
                str(finding.get("summary", ""))
                for finding in findings
                if str(finding.get("summary", "")).strip()
            ]
            if not reasons:
                reasons = [f"{table_name} has sampled data that looks suspicious for normalization review."]

            candidate_id = re.sub(r"[^a-zA-Z0-9_]+", "_", table_name).strip("_").lower()
            candidates.append(
                {
                    "id": candidate_id,
                    "source_table": table_name,
                    "status": "pending",
                    "normal_forms": ["1NF"],
                    "suspicious_columns": columns,
                    "source_column_count": len(source_table.columns) if source_table is not None else 0,
                    "suspicion_reasons": reasons,
                    "proposal_summary": (
                        f"Split {', '.join(columns)} out of {table_name} into child table(s), "
                        "while keeping the remaining columns in a copied parent table."
                    ),
                    "findings": findings,
                    "proposed_target_tables": relevant_targets,
                }
            )
        return candidates

    def _plan_target_schema_step(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        normalization_step = next((step for step in record.steps if step.name == "propose_3nf_schema"), None)
        if normalization_step is None:
            return self._mark_planned(
                record,
                "create_target_schema",
                {"summary": "Normalization proposal step was not found."},
            )

        sql_statements = normalization_step.details.get("sql_statements", [])
        target_tables = normalization_step.details.get("target_tables", [])
        summary = normalization_step.details.get("summary", "")

        if normalization_step.details.get("review_status") == "pending":
            return self._mark_planned(
                record,
                "create_target_schema",
                {
                    "summary": "Target-schema DDL is waiting for table-by-table normalization review decisions.",
                    "normalization_summary": summary,
                    "pending_review_count": sum(
                        1
                        for review in normalization_step.details.get("normalization_reviews", [])
                        if review.get("status") == "pending"
                    ),
                },
            )

        if sql_statements:
            ddl_path = self._store.write_text_artifact(
                record.run_id,
                "create_target_schema",
                "\n\n".join(sql_statements) + "\n",
                suffix=".sql",
            )
            record.artifacts["create_target_schema_sql"] = ddl_path
            return self._mark_planned(
                record,
                "create_target_schema",
                {
                    "summary": f"Approval-gated SQL is ready for {self._config.postgres.new_database} schema creation.",
                    "normalization_summary": summary,
                    "target_table_count": len(target_tables),
                    "sql_statement_count": len(sql_statements),
                    "sql_statements": sql_statements,
                    "sql_path": ddl_path,
                },
            )

        return self._mark_planned(
            record,
            "create_target_schema",
            {
                "summary": "No target-schema SQL was generated because the proposal did not recommend a decomposition.",
                "normalization_summary": summary,
            },
        )

    def _plan_migration_step(self, record: BenchmarkRunRecord, schema: SchemaSnapshot | None) -> BenchmarkRunRecord:
        if schema is None:
            return self._fail_step(record, "migrate_data", "Migration planning is blocked until schema inspection succeeds.")

        normalization_step = next((step for step in record.steps if step.name == "propose_3nf_schema"), None)
        target_tables = normalization_step.details.get("target_tables", []) if normalization_step else []
        try:
            proposal = self._migration_planner.plan(schema, target_tables)
            proposal_dict = {
                "status": proposal.status,
                "summary": proposal.summary,
                "reasoning": proposal.rationale,
                "statements": proposal.statements,
                "target_tables": target_tables,
                "source": proposal.source,
            }
            if proposal.statements:
                migration_sql = "\n\n".join(statement["sql"] for statement in proposal.statements) + "\n"
                migration_path = self._store.write_text_artifact(
                    record.run_id,
                    "migration_plan",
                    migration_sql,
                    suffix=".sql",
                )
                proposal_dict["sql_path"] = migration_path
                record.artifacts["migration_plan_sql"] = migration_path
            record.artifacts["migrate_data"] = self._store.write_artifact(
                record.run_id,
                "migration_plan",
                proposal_dict | {"raw_response_text": proposal.raw_response_text},
            )
            if proposal.statements:
                return self._mark_planned(record, "migrate_data", proposal_dict)
            return self._complete_step(record, "migrate_data", proposal_dict)
        except Exception as exc:
            return self._fail_step(record, "migrate_data", str(exc))

    def _plan_rewrite_queries_step(self, record: BenchmarkRunRecord, log_summary) -> BenchmarkRunRecord:
        normalization_step = next((step for step in record.steps if step.name == "propose_3nf_schema"), None)
        migrate_step = next((step for step in record.steps if step.name == "migrate_data"), None)
        target_tables = normalization_step.details.get("target_tables", []) if normalization_step else []
        migration_statements = migrate_step.details.get("statements", []) if migrate_step else []
        if not target_tables:
            target_tables = self._existing_target_tables()

        try:
            workload_sql = self._load_source_workload_text(log_summary)
        except Exception as exc:
            return self._fail_step(record, "rewrite_queries", str(exc))

        try:
            proposal = self._workload_planner.rewrite(workload_sql, target_tables, migration_statements)
            proposal_dict = {
                "status": proposal.status,
                "summary": proposal.summary,
                "reasoning": proposal.rationale,
                "statement_count": len(proposal.statements),
                "statements": proposal.statements,
                "source": proposal.source,
                "target_table_count": len(target_tables),
            }
            if proposal.statements:
                validation_errors = self._validate_db_new_workload(proposal.statements)
                if validation_errors:
                    sample_errors = " | ".join(
                        str(item.get("error", "")).splitlines()[0]
                        for item in validation_errors[:3]
                    )
                    raise ValueError(
                        f"Generated {self._config.postgres.new_database} workload does not validate against the current "
                        f"{self._config.postgres.new_database} schema. "
                        f"{sample_errors}"
                    )
                sql_text = "\n\n".join(proposal.statements) + "\n"
                sql_path = self._store.write_text_artifact(
                    record.run_id,
                    "rewritten_workload_db_new",
                    sql_text,
                    suffix=".sql",
                )
                proposal_dict["workload_path"] = sql_path
                record.artifacts["rewrite_queries"] = self._store.write_artifact(
                    record.run_id,
                    "rewritten_workload_db_new",
                    proposal_dict | {"raw_response_text": proposal.raw_response_text},
                )
                return self._complete_step(record, "rewrite_queries", proposal_dict)
            return self._complete_step(record, "rewrite_queries", proposal_dict)
        except Exception as exc:
            return self._fail_step(record, "rewrite_queries", str(exc))

    def _existing_target_tables(self) -> list[dict]:
        try:
            if not self._postgres.database_exists(self._config.postgres.new_database):
                return []
            schema = self._postgres.inspect_schema(self._config.postgres.new_database)
        except Exception:
            return []
        return [
            {
                "name": table.name,
                "purpose": f"Existing {self._config.postgres.new_database} table detected from PostgreSQL metadata.",
                "source_tables": [],
                "columns": [
                    {
                        "source_table": "",
                        "source_column": column.name,
                        "name": column.name,
                    }
                    for column in table.columns
                ],
                "primary_key": [
                    column
                    for constraint in table.unique_constraints
                    for column in constraint.columns
                ][:1],
                "uniques": [constraint.columns for constraint in table.unique_constraints],
                "foreign_keys": [
                    {
                        "columns": foreign_key.columns,
                        "references_table": foreign_key.referenced_table.split(".")[-1],
                        "references_columns": foreign_key.referenced_columns,
                    }
                    for foreign_key in table.foreign_keys
                ],
            }
            for table in schema.tables
        ]

    def _validate_db_new_workload(self, statements: list[str]) -> list[dict]:
        try:
            target_exists = self._postgres.database_exists(self._config.postgres.new_database)
        except Exception as exc:
            # Planning can run before PostgreSQL is available; validation is best-effort then.
            return []
        if not target_exists:
            return []
        try:
            return self._postgres.validate_workload_statements(
                self._config.postgres.new_database,
                statements,
            )
        except Exception as exc:
            return [{"statement": "", "error": str(exc)}]

    def _split_sql_statements(self, text: str) -> list[str]:
        return [
            statement.strip() + ";"
            for statement in text.split(";")
            if statement.strip()
        ]

    def _plan_index_recommendations_step(
        self,
        record: BenchmarkRunRecord,
        schema: SchemaSnapshot | None,
        log_summary: LogSummary | None,
    ) -> BenchmarkRunRecord:
        normalization_step = next((step for step in record.steps if step.name == "propose_3nf_schema"), None)
        target_tables = normalization_step.details.get("target_tables", []) if normalization_step else []
        try:
            workload_sql = self._load_rewritten_or_source_workload_text(record, log_summary)
            plan = self._index_advisor.recommend(
                schema=schema,
                target_tables=target_tables,
                workload_sql=workload_sql,
                log_summary=log_summary,
            )
            recommendations = [to_dict(item) for item in plan.recommendations]
            plan_dict = {
                "status": plan.status,
                "summary": plan.summary,
                "source": plan.source,
                "recommendation_count": len(recommendations),
                "recommendations": recommendations,
                "sql_statements": [item["sql"] for item in recommendations],
            }
            record.artifacts["optimize_indexes"] = self._store.write_artifact(
                record.run_id,
                "index_recommendations",
                plan_dict,
            )
            record = self._plan_secondary_index_creation_step(record, plan_dict)
            return self._complete_step(record, "optimize_indexes", plan_dict)
        except Exception as exc:
            return self._fail_step(record, "optimize_indexes", str(exc))

    def _plan_secondary_index_creation_step(self, record: BenchmarkRunRecord, index_plan: dict) -> BenchmarkRunRecord:
        statements = list(index_plan.get("sql_statements", []))
        if statements:
            return self._mark_planned(
                record,
                "create_secondary_indexes",
                {
                    "summary": (
                        f"Approval-gated: create recommended secondary indexes on {self._config.postgres.new_database} "
                        "after the target schema exists."
                    ),
                    "recommendation_count": len(statements),
                    "sql_statements": statements,
                },
            )

        return self._complete_step(
            record,
            "create_secondary_indexes",
            {
                "summary": "No secondary indexes were recommended for creation.",
                "recommendation_count": 0,
                "sql_statements": [],
            },
        )

    def _run_tuning_step(
        self,
        record: BenchmarkRunRecord,
        log_summary: LogSummary | None,
    ) -> BenchmarkRunRecord:
        try:
            hardware = self._load_optional_artifact(record, "capture_hardware") or {}
            plan = self._tuning_advisor.recommend(
                hardware=hardware,
                log_summary=log_summary,
                pgbench=self._config.pgbench,
            )
            recommendations = [to_dict(item) for item in plan.recommendations]
            plan_dict = {
                "status": plan.status,
                "summary": plan.summary,
                "source": plan.source,
                "recommendation_count": len(recommendations),
                "recommendations": recommendations,
                "postgresql_conf_lines": [
                    f"{item['setting']} = '{item['recommended_value']}'"
                    for item in recommendations
                ],
                "alter_system_statements": [
                    f"ALTER SYSTEM SET {item['setting']} = '{item['recommended_value']}';"
                    for item in recommendations
                ],
            }
            record.artifacts["tune_postgresql_conf"] = self._store.write_artifact(
                record.run_id,
                "postgresql_tuning",
                plan_dict,
            )
            return self._complete_step(record, "tune_postgresql_conf", plan_dict)
        except Exception as exc:
            return self._fail_step(record, "tune_postgresql_conf", str(exc))

    def _run_extended_metrics_step(
        self,
        record: BenchmarkRunRecord,
        databases: list[str],
    ) -> BenchmarkRunRecord:
        unique_databases = list(dict.fromkeys(database for database in databases if database))
        metrics: dict[str, dict] = {}
        if not hasattr(self._postgres, "collect_database_metrics"):
            payload = {
                "status": "unavailable",
                "summary": "The configured PostgreSQL tool does not expose advanced metrics collection.",
                "databases": metrics,
            }
            record.artifacts["collect_extended_metrics"] = self._store.write_artifact(
                record.run_id,
                "extended_metrics",
                payload,
            )
            return self._complete_step(record, "collect_extended_metrics", payload)

        for database in unique_databases:
            try:
                metrics[database] = self._postgres.collect_database_metrics(database)
                metrics[database]["status"] = "completed"
            except Exception as exc:
                metrics[database] = {
                    "database": database,
                    "status": "failed",
                    "error": str(exc),
                }

        completed = sum(1 for item in metrics.values() if item.get("status") == "completed")
        failed = sum(1 for item in metrics.values() if item.get("status") == "failed")
        status = "completed" if completed and not failed else "partial" if completed else "unavailable"
        payload = {
            "status": status,
            "summary": f"Collected advanced metrics for {completed} database(s); {failed} collection attempt(s) failed.",
            "databases": metrics,
        }
        record.artifacts["collect_extended_metrics"] = self._store.write_artifact(
            record.run_id,
            "extended_metrics",
            payload,
        )
        return self._complete_step(record, "collect_extended_metrics", payload)

    def _plan_pgbench_original_step(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        workload_path = self._resolve_workload_path()
        workload_exists = workload_path is not None and workload_path.exists()
        return self._mark_planned(
            record,
            "run_pgbench_original",
            {
                "summary": (
                    f"Approval-gated: benchmark {self._config.postgres.original_database} with pgbench for the configured duration."
                    if workload_exists
                    else f"Provide a workload SQL file before benchmarking {self._config.postgres.original_database}."
                ),
                "workload_path": str(workload_path) if workload_exists else "",
                "duration_seconds": self._config.pgbench.duration_seconds,
                "clients": self._config.pgbench.clients,
                "jobs": self._config.pgbench.jobs,
                "enabled": self._config.pgbench.enabled,
            },
        )

    def _plan_pgbench_new_step(self, record: BenchmarkRunRecord) -> BenchmarkRunRecord:
        rewrite_step = next((step for step in record.steps if step.name == "rewrite_queries"), None)
        workload_path = str(rewrite_step.details.get("workload_path", "")).strip() if rewrite_step else ""
        return self._mark_planned(
            record,
            "run_pgbench_new",
            {
                "summary": (
                    f"Approval-gated: benchmark {self._config.postgres.new_database} with the rewritten workload after schema creation and migration."
                    if workload_path
                    else f"Rewritten {self._config.postgres.new_database} workload is not ready yet, so benchmarking cannot start."
                ),
                "workload_path": workload_path,
                "duration_seconds": self._config.pgbench.duration_seconds,
                "clients": self._config.pgbench.clients,
                "jobs": self._config.pgbench.jobs,
                "enabled": self._config.pgbench.enabled,
            },
        )

    def _pipeline_template(self) -> list[StepResult]:
        return [
            StepResult(name="llm_gateway", title="Test LLM gateway", status="pending"),
            StepResult(name="inspect_source_schema", title="Inspect source PostgreSQL schema", status="pending"),
            StepResult(name="scan_first_normal_form", title="Scan sampled data for 1NF warnings", status="pending"),
            StepResult(name="extract_workload_logs", title="Extract workload from PostgreSQL logs or workload file", status="pending"),
            StepResult(name="propose_3nf_schema", title="Propose 3NF normalization plan", status="pending"),
            StepResult(name="create_target_schema", title=f"Create {self._config.postgres.new_database} schema", status="pending", requires_approval=True),
            StepResult(name="migrate_data", title=f"Migrate data into {self._config.postgres.new_database}", status="pending", requires_approval=True),
            StepResult(name="rewrite_queries", title=f"Rewrite workload for {self._config.postgres.new_database}", status="pending"),
            StepResult(name="suggest_summary_tables", title="Suggest summary tables", status="pending"),
            StepResult(name="optimize_indexes", title="Recommend workload-aware indexes", status="pending"),
            StepResult(name="create_secondary_indexes", title="Create secondary indexes", status="pending", requires_approval=True),
            StepResult(name="capture_hardware", title="Capture hardware snapshot", status="pending"),
            StepResult(name="tune_postgresql_conf", title="Recommend postgresql.conf tuning", status="pending"),
            StepResult(name="run_pgbench_original", title=f"Run pgbench against {self._config.postgres.original_database}", status="pending", requires_approval=True),
            StepResult(name="run_pgbench_new", title=f"Run pgbench against {self._config.postgres.new_database}", status="pending", requires_approval=True),
            StepResult(name="compare_disk_usage", title="Compare before/after benchmark results", status="pending"),
            StepResult(name="collect_extended_metrics", title="Collect advanced PostgreSQL metrics", status="pending"),
        ]

    def _complete_step(self, record: BenchmarkRunRecord, name: str, details: dict) -> BenchmarkRunRecord:
        return self._replace_step(record, name, status="completed", details=details, error=None, planned_only=False)

    def _mark_planned(self, record: BenchmarkRunRecord, name: str, details: dict) -> BenchmarkRunRecord:
        return self._replace_step(record, name, status="planned", details=details, error=None, planned_only=True)

    def _fail_step(self, record: BenchmarkRunRecord, name: str, error: str) -> BenchmarkRunRecord:
        return self._replace_step(record, name, status="failed", details={}, error=error, planned_only=False)

    def _input_required_step(self, record: BenchmarkRunRecord, name: str, details: dict) -> BenchmarkRunRecord:
        return self._replace_step(record, name, status="input_required", details=details, error=None, planned_only=False)

    def _replace_step(
        self,
        record: BenchmarkRunRecord,
        name: str,
        *,
        status: str,
        details: dict,
        error: str | None,
        planned_only: bool,
    ) -> BenchmarkRunRecord:
        new_steps: list[StepResult] = []
        for step in record.steps:
            if step.name == name:
                new_steps.append(
                    replace(
                        step,
                        status=status,
                        details=details,
                        error=error,
                        planned_only=planned_only,
                    )
                )
            else:
                new_steps.append(step)
        record.steps = new_steps
        record.updated_at = datetime.now(UTC).isoformat()
        self._store.upsert_run(record)
        return record

    def _load_schema_artifact(self, record: BenchmarkRunRecord) -> SchemaSnapshot:
        target = self._resolve_run_artifact_path(record, "inspect_source_schema")
        if target is None:
            raise ValueError("Schema artifact is missing from the run.")
        payload = json.loads(target.read_text(encoding="utf-8"))
        return self._schema_snapshot_from_payload(payload)

    def _load_optional_artifact(self, record: BenchmarkRunRecord, name: str) -> dict | None:
        target = self._resolve_run_artifact_path(record, name)
        if target is None:
            return None
        return json.loads(target.read_text(encoding="utf-8"))

    def _load_log_summary_artifact(self, record: BenchmarkRunRecord) -> LogSummary | None:
        target = self._resolve_run_artifact_path(record, "extract_workload_logs")
        if target is None:
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
        return LogSummary(
            path=str(payload.get("path", "")),
            lines_processed=int(payload.get("lines_processed", 0)),
            statements_detected=int(payload.get("statements_detected", 0)),
            transactions_detected=int(payload.get("transactions_detected", 0)),
            multi_statement_transactions=int(payload.get("multi_statement_transactions", 0)),
            top_queries=[
                QueryObservation(
                    fingerprint=str(item.get("fingerprint", "")),
                    sample_sql=str(item.get("sample_sql", "")),
                    count=int(item.get("count", 0)),
                    total_duration_ms=float(item.get("total_duration_ms", 0.0)),
                )
                for item in payload.get("top_queries", [])
            ],
            sampled_transactions=[
                TransactionObservation(
                    pid=str(item.get("pid", "")),
                    statement_count=int(item.get("statement_count", 0)),
                    statements=[str(statement) for statement in item.get("statements", [])],
                )
                for item in payload.get("sampled_transactions", [])
            ],
            source_kind=str(payload.get("source_kind", "postgres_log")),
        )

    def _resolve_run_artifact_path(self, record: BenchmarkRunRecord, name: str) -> Path | None:
        artifact_path = record.artifacts.get(name)
        if not artifact_path:
            return None

        target = Path(artifact_path)
        if target.exists():
            return target

        filename = PureWindowsPath(artifact_path).name
        if not filename or filename == artifact_path:
            filename = target.name
        relocated = self._config.resolve_path(self._config.storage.artifact_dir) / record.run_id / filename
        if relocated.exists():
            return relocated
        return None

    def _schema_snapshot_from_payload(self, payload: dict) -> SchemaSnapshot:
        from ilvesbench.models import ColumnMetadata, ForeignKeyMetadata, IndexMetadata, TableMetadata, UniqueConstraintMetadata

        tables = []
        for table in payload.get("tables", []):
            tables.append(
                TableMetadata(
                    schema=table["schema"],
                    name=table["name"],
                    columns=[
                        ColumnMetadata(
                            name=column["name"],
                            data_type=column["data_type"],
                            is_nullable=column["is_nullable"],
                            default=column.get("default"),
                        )
                        for column in table.get("columns", [])
                    ],
                    indexes=[
                        IndexMetadata(
                            name=index["name"],
                            definition=index["definition"],
                            is_unique=index.get("is_unique", False),
                        )
                        for index in table.get("indexes", [])
                    ],
                    foreign_keys=[
                        ForeignKeyMetadata(
                            name=foreign_key["name"],
                            columns=list(foreign_key.get("columns", [])),
                            referenced_table=foreign_key["referenced_table"],
                            referenced_columns=list(foreign_key.get("referenced_columns", [])),
                        )
                        for foreign_key in table.get("foreign_keys", [])
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(
                            name=constraint["name"],
                            columns=list(constraint.get("columns", [])),
                        )
                        for constraint in table.get("unique_constraints", [])
                    ],
                )
            )
        return SchemaSnapshot(
            database=payload["database"],
            collected_at=payload["collected_at"],
            tables=tables,
            database_size_bytes=payload.get("database_size_bytes"),
        )

    def _source_tables_for_migration(self, schema: SchemaSnapshot, target_tables: list[dict]):
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

    def _resolve_workload_path(self) -> Path | None:
        workload_value = (self._config.workload.path or "").strip() or "data/workload.sql"
        return self._config.resolve_path(workload_value)

    def _load_source_workload_text(self, log_summary) -> str:
        workload_path = self._resolve_workload_path()
        if workload_path is not None and workload_path.exists():
            return workload_path.read_text(encoding="utf-8", errors="replace")
        if log_summary is None:
            raise ValueError(f"No workload source was available to rewrite for {self._config.postgres.new_database}.")

        statements: list[str] = []
        for query in log_summary.top_queries:
            repetitions = max(1, min(query.count, 20))
            sample = query.sample_sql.strip().rstrip(";")
            if not sample:
                continue
            for _ in range(repetitions):
                statements.append(sample + ";")
        if not statements:
            raise ValueError(f"No executable SQL statements were available to rewrite for {self._config.postgres.new_database}.")
        return "\n\n".join(statements) + "\n"

    def _load_rewritten_or_source_workload_text(self, record: BenchmarkRunRecord, log_summary) -> str:
        rewritten_path = self._rewritten_workload_path(record)
        if rewritten_path is not None and rewritten_path.exists():
            return rewritten_path.read_text(encoding="utf-8", errors="replace")
        return self._load_source_workload_text(log_summary)

    def _rewritten_workload_path(self, record: BenchmarkRunRecord) -> Path | None:
        rewrite_step = next((step for step in record.steps if step.name == "rewrite_queries"), None)
        if rewrite_step is None:
            return None
        workload_path = str(rewrite_step.details.get("workload_path", "")).strip()
        if not workload_path:
            return None
        return Path(workload_path).resolve()

    def _storage_comparison(self, record: BenchmarkRunRecord) -> dict | None:
        metrics = self._load_optional_artifact(record, "collect_extended_metrics") or {}
        databases = metrics.get("databases", {})
        original = databases.get(self._config.postgres.original_database, {})
        normalized = databases.get(self._config.postgres.new_database, {})
        original_size = original.get("database_size_bytes")
        normalized_size = normalized.get("database_size_bytes")
        if original_size is None or normalized_size is None:
            return None
        try:
            original_size_int = int(original_size)
            normalized_size_int = int(normalized_size)
        except (TypeError, ValueError):
            return None
        delta = normalized_size_int - original_size_int
        percent = round((delta / original_size_int) * 100, 3) if original_size_int else None
        return {
            "original_database_size_bytes": original_size_int,
            "normalized_database_size_bytes": normalized_size_int,
            "size_delta_bytes": delta,
            "size_change_percent": percent,
        }

    def _pgbench_error_summary(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        fatal_lines = [
            line
            for line in lines
            if line.lower().startswith("pgbench: error:")
            or line.lower().startswith("error:")
            or line.lower().startswith("fatal:")
        ]
        if fatal_lines:
            return " | ".join(fatal_lines[:3])
        return lines[-1][:500]

    def _overall_status(self, record: BenchmarkRunRecord) -> str:
        if any(step.status == "running" for step in record.steps):
            return "running"
        if any(step.status == "failed" for step in record.steps if step.name == "create_target_schema"):
            return "awaiting_input"
        if any(step.status == "failed" for step in record.steps if step.name in {"create_target_schema", "migrate_data"}):
            return "failed"
        if any(step.status == "input_required" for step in record.steps):
            return "awaiting_input"
        return "completed"

    def _summary_counts(self, record: BenchmarkRunRecord) -> dict:
        return {
            "original_database": self._config.postgres.original_database,
            "new_database": self._config.postgres.new_database,
            "normalization_status": next(
                (step.details.get("status") for step in record.steps if step.name == "propose_3nf_schema"),
                "",
            ),
            "normalization_summary": next(
                (step.details.get("summary") for step in record.steps if step.name == "propose_3nf_schema"),
                "",
            ),
            "completed_steps": sum(1 for step in record.steps if step.status == "completed"),
            "planned_steps": sum(1 for step in record.steps if step.status == "planned"),
            "failed_steps": sum(1 for step in record.steps if step.status == "failed"),
            "input_required_steps": sum(1 for step in record.steps if step.status == "input_required"),
        }
