from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.agent.orchestrator import PipelineOrchestrator
from ilvesbench.benchmark.migration_planner import MigrationPlanner
from ilvesbench.benchmark.schema_transformer import SchemaTransformer
from ilvesbench.benchmark.workload import WorkloadPlanner
from ilvesbench.config import IlvesBenchConfig
from ilvesbench.models import ColumnMetadata, LLMResult, SchemaSnapshot, TableMetadata, UniqueConstraintMetadata, to_dict


class StaticGateway:
    def __init__(self, response_text: str = "hello") -> None:
        self._response_text = response_text

    def generate(self, messages, model=None, max_tokens=256) -> LLMResult:
        return LLMResult(
            backend="aviary",
            model=model or "fake-model",
            response_text=self._response_text,
            raw_response={},
        )


class FailingPostgres:
    def create_database_if_missing(self, database: str) -> str:
        return "created"

    def execute_statements(self, database: str, statements: list[str]) -> list[dict]:
        raise RuntimeError('there is no unique constraint matching given keys for referenced table "student_course"')


class FailingPgBench:
    def run(self, config, postgres, database, workload_path=None):
        from ilvesbench.models import BenchmarkMetrics

        return BenchmarkMetrics(
            database=database,
            status="failed",
            command=["pgbench"],
            duration_seconds=config.duration_seconds,
            clients=config.clients,
            jobs=config.jobs,
            transactions=config.transactions,
            stdout="verbose line 1\nverbose line 2\n",
            stderr=(
                "some setup line\n"
                "pgbench: error: client 0 script 0 aborted in command 1 query 0: ERROR: operator does not exist: character varying = integer\n"
                "LINE 3: WHERE course_code = 1;\n"
                "another trailing line\n"
            ),
        )


class FakeOrchestrator(PipelineOrchestrator):
    def __init__(self, config: IlvesBenchConfig, normalization_response_text: str | None = None) -> None:
        super().__init__(config)
        self._llm = StaticGateway()
        response_text = normalization_response_text or json.dumps(
            {
                "assessment": {
                    "status": "appears_3nf",
                    "summary": "No decomposition recommended.",
                    "reasoning": ["Synthetic test response."],
                },
                "table_findings": [],
                "functional_dependencies": [],
                "target_tables": [],
                "decomposition_summary": "No decomposition recommended.",
            }
        )
        self._schema_transformer = SchemaTransformer(
            llm=StaticGateway(response_text),
            target_database=config.postgres.new_database,
        )
        self._migration_planner = MigrationPlanner(
            llm=StaticGateway(
                json.dumps(
                    {
                        "status": "no_migration_needed",
                        "summary": "No migration needed for synthetic test schema.",
                        "reasoning": ["Synthetic test response."],
                        "statements": [],
                    }
                )
            )
        )
        self._workload_planner = WorkloadPlanner(
            llm=StaticGateway(
                json.dumps(
                    {
                        "status": "planned",
                        "summary": "Synthetic rewritten workload.",
                        "reasoning": ["Synthetic test response."],
                        "statements": ["SELECT id, name FROM items_lookup WHERE id = 1"],
                    }
                )
            )
        )
        self._fake_schema = SchemaSnapshot(
            database=config.postgres.original_database,
            collected_at="2026-04-23T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="items",
                    columns=[
                        ColumnMetadata(name="id", data_type="integer", is_nullable=False),
                        ColumnMetadata(name="name", data_type="text", is_nullable=False),
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="items_pkey", columns=["id"]),
                    ],
                )
            ],
        )

    def test_llm(self) -> dict:
        return {
            "backend": "aviary",
            "model": "fake-model",
            "response_text": "hello",
            "raw_response": {},
        }

    def _run_schema_step(self, record):
        schema_dict = to_dict(self._fake_schema)
        record.artifacts["inspect_source_schema"] = self.store.write_artifact(
            record.run_id,
            "schema_snapshot",
            schema_dict,
        )
        self._complete_step(
            record,
            "inspect_source_schema",
            {
                "database": self._fake_schema.database,
                "table_count": len(self._fake_schema.tables),
                "database_size_bytes": self._fake_schema.database_size_bytes,
            },
        )
        return self._fake_schema

class OrchestratorTests(unittest.TestCase):
    def test_run_requests_human_input_when_no_workload_source_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.toml"
            config_path.write_text(
                """
                [llm]
                backend = "aviary"
                base_url = "https://example.invalid/v1/chat/completions"
                model = "fake-model"

                [postgres]
                original_database = "source_db"
                new_database = "target_db"

                [logs]
                path = "missing.log"

                [pgbench]
                enabled = false

                [storage]
                sqlite_path = "runs.sqlite3"
                artifact_dir = "artifacts"
                """,
                encoding="utf-8",
            )
            config = IlvesBenchConfig.from_toml(config_path)
            orchestrator = FakeOrchestrator(config)
            result = orchestrator.run_mvp_collection()

            self.assertEqual(result.status, "awaiting_input")
            saved = orchestrator.store.get_run(result.run_id)
            self.assertIsNotNone(saved)
            self.assertEqual(saved["run_id"], result.run_id)
            workload_step = next(step for step in saved["steps"] if step["name"] == "extract_workload_logs")
            self.assertEqual(workload_step["status"], "input_required")

    def test_run_uses_workload_file_when_log_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workload_path = root / "workload.sql"
            workload_path.write_text(
                "SELECT * FROM users WHERE id = 1; UPDATE users SET active = true WHERE id = 1;",
                encoding="utf-8",
            )
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
                [llm]
                backend = "aviary"
                base_url = "https://example.invalid/v1/chat/completions"
                model = "fake-model"

                [postgres]
                original_database = "source_db"
                new_database = "target_db"

                [logs]
                path = "missing.log"

                [workload]
                path = "{workload_path.name}"

                [pgbench]
                enabled = false

                [storage]
                sqlite_path = "runs.sqlite3"
                artifact_dir = "artifacts"
                """,
                encoding="utf-8",
            )
            config = IlvesBenchConfig.from_toml(config_path)
            orchestrator = FakeOrchestrator(config)
            result = orchestrator.run_mvp_collection()

            self.assertEqual(result.status, "completed")
            saved = orchestrator.store.get_run(result.run_id)
            self.assertIsNotNone(saved)
            workload_step = next(step for step in saved["steps"] if step["name"] == "extract_workload_logs")
            self.assertEqual(workload_step["status"], "completed")
            self.assertEqual(workload_step["details"]["source_kind"], "workload_file")
            rewrite_step = next(step for step in saved["steps"] if step["name"] == "rewrite_queries")
            self.assertEqual(rewrite_step["status"], "completed")
            self.assertGreaterEqual(rewrite_step["details"]["statement_count"], 1)
            self.assertTrue(rewrite_step["details"]["workload_path"].endswith(".sql"))
            pgbench_step = next(step for step in saved["steps"] if step["name"] == "run_pgbench_original")
            self.assertEqual(pgbench_step["status"], "planned")
            self.assertTrue(pgbench_step["details"]["workload_path"].endswith(".sql"))

    def test_schema_creation_failure_becomes_actionable_review_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workload_path = root / "workload.sql"
            workload_path.write_text("SELECT 1;", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
                [llm]
                backend = "aviary"
                base_url = "https://example.invalid/v1/chat/completions"
                model = "fake-model"

                [postgres]
                original_database = "source_db"
                new_database = "target_db"

                [logs]
                path = "missing.log"

                [workload]
                path = "{workload_path.name}"

                [pgbench]
                enabled = false

                [storage]
                sqlite_path = "runs.sqlite3"
                artifact_dir = "artifacts"
                """,
                encoding="utf-8",
            )
            config = IlvesBenchConfig.from_toml(config_path)
            normalization_response = json.dumps(
                {
                    "assessment": {
                        "status": "candidate_normalization",
                        "summary": "Normalized from 1 table to 1 target table for test coverage.",
                        "reasoning": ["Synthetic repairability test."],
                    },
                    "table_findings": ["Synthetic finding."],
                    "functional_dependencies": [],
                    "target_tables": [
                        {
                            "name": "items_lookup",
                            "purpose": "Synthetic target table.",
                            "source_tables": ["public.items"],
                            "columns": [
                                {"source_table": "public.items", "source_column": "id", "name": "id"},
                                {"source_table": "public.items", "source_column": "name", "name": "name"},
                            ],
                            "primary_key": ["id"],
                            "uniques": [["name"]],
                            "foreign_keys": [],
                        }
                    ],
                    "decomposition_summary": "Normalized from 1 table to 1 target table.",
                }
            )
            orchestrator = FakeOrchestrator(config, normalization_response_text=normalization_response)
            orchestrator._postgres = FailingPostgres()

            record = orchestrator.run_mvp_collection()
            orchestrator.begin_create_target_schema(record.run_id)
            result = orchestrator.execute_create_target_schema(record.run_id)

            self.assertEqual(result.status, "awaiting_input")
            create_step = next(step for step in result.steps if step.name == "create_target_schema")
            self.assertEqual(create_step.status, "failed")
            self.assertIn("repair the DDL with the LLM", create_step.details["summary"])
            self.assertIn("unique constraint matching given keys", create_step.error)

    def test_pgbench_failure_is_compacted_to_fatal_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workload_path = root / "workload.sql"
            workload_path.write_text("SELECT 1;", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
                [llm]
                backend = "aviary"
                base_url = "https://example.invalid/v1/chat/completions"
                model = "fake-model"

                [postgres]
                original_database = "source_db"
                new_database = "target_db"

                [logs]
                path = "missing.log"

                [workload]
                path = "{workload_path.name}"

                [pgbench]
                enabled = true

                [storage]
                sqlite_path = "runs.sqlite3"
                artifact_dir = "artifacts"
                """,
                encoding="utf-8",
            )
            config = IlvesBenchConfig.from_toml(config_path)
            orchestrator = FakeOrchestrator(config)
            orchestrator._pgbench = FailingPgBench()

            record = orchestrator.run_mvp_collection()
            orchestrator.begin_pgbench_original(record.run_id)
            result = orchestrator.execute_pgbench_original(record.run_id)

            pgbench_step = next(step for step in result.steps if step.name == "run_pgbench_original")
            self.assertEqual(pgbench_step.status, "failed")
            self.assertIn("pgbench: error:", pgbench_step.error)
            self.assertIn("fatal_error", pgbench_step.details)
            self.assertNotIn("stderr", pgbench_step.details)
            self.assertNotIn("command", pgbench_step.details)

    def test_regenerate_rewrite_resets_db_new_benchmark_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workload_path = root / "workload.sql"
            workload_path.write_text("SELECT 1;", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
                [llm]
                backend = "aviary"
                base_url = "https://example.invalid/v1/chat/completions"
                model = "fake-model"

                [postgres]
                original_database = "source_db"
                new_database = "target_db"

                [logs]
                path = "missing.log"

                [workload]
                path = "{workload_path.name}"

                [pgbench]
                enabled = false

                [storage]
                sqlite_path = "runs.sqlite3"
                artifact_dir = "artifacts"
                """,
                encoding="utf-8",
            )
            config = IlvesBenchConfig.from_toml(config_path)
            normalization_response = json.dumps(
                {
                    "assessment": {
                        "status": "candidate_normalization",
                        "summary": "Normalized from 1 table to 1 target table.",
                        "reasoning": ["Synthetic rewrite test."],
                    },
                    "table_findings": [],
                    "functional_dependencies": [],
                    "target_tables": [
                        {
                            "name": "items_lookup",
                            "purpose": "Synthetic target table.",
                            "source_tables": ["public.items"],
                            "columns": [
                                {"source_table": "public.items", "source_column": "id", "name": "id"},
                                {"source_table": "public.items", "source_column": "name", "name": "name"},
                            ],
                            "primary_key": ["id"],
                            "uniques": [],
                            "foreign_keys": [],
                        }
                    ],
                    "decomposition_summary": "Normalized from 1 table to 1 target table.",
                }
            )
            orchestrator = FakeOrchestrator(config, normalization_response_text=normalization_response)

            result = orchestrator.run_mvp_collection()
            result = orchestrator._replace_step(
                result,
                "run_pgbench_new",
                status="failed",
                details={"workload_path": "/tmp/bad.sql"},
                error="bad workload",
                planned_only=False,
            )
            orchestrator.store.upsert_run(result)

            orchestrator.begin_regenerate_rewrite_queries(result.run_id)
            updated = orchestrator.execute_regenerate_rewrite_queries(result.run_id)

            rewrite_step = next(step for step in updated.steps if step.name == "rewrite_queries")
            pgbench_new_step = next(step for step in updated.steps if step.name == "run_pgbench_new")
            self.assertEqual(rewrite_step.status, "completed")
            self.assertTrue(rewrite_step.details["workload_path"].endswith(".sql"))
            self.assertEqual(pgbench_new_step.status, "planned")
            self.assertEqual(pgbench_new_step.details["workload_path"], rewrite_step.details["workload_path"])


if __name__ == "__main__":
    unittest.main()
