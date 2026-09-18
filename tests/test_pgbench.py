from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.benchmark.pgbench import PgBenchParameterAdvisor, PgBenchRunner
from ilvesbench.benchmark.workload import WorkloadRewriteProposal
from ilvesbench.benchmarker.query_rewrite import QueryRewriteExecutionError, QueryRewriteService
from ilvesbench.benchmarker.service import BenchmarkerService
from ilvesbench.config import IlvesBenchConfig, PgBenchConfig, PostgresConfig, StorageConfig
from ilvesbench.models import BenchmarkMetrics, HardwareSnapshot


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.last_args = None
        self.last_timeout = None
        self.last_env = None

    def which(self, command: str) -> str | None:
        return f"/usr/bin/{command}"

    def run(self, args: list[str], timeout: int = 30, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.last_args = args
        self.last_timeout = timeout
        self.last_env = env
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr=self.stderr)


class FakePgBench:
    def __init__(self) -> None:
        self.calls = []

    def run(self, config, postgres, database, workload_path=None):
        self.calls.append((config, postgres, database, workload_path))
        return BenchmarkMetrics(
            database=database,
            status="completed",
            command=["pgbench"],
            duration_seconds=config.duration_seconds,
            clients=config.clients,
            jobs=config.jobs,
            transactions=config.transactions,
        )


class PgBenchTests(unittest.TestCase):
    def test_pgbench_parameter_advisor_uses_hardware_as_starting_point(self) -> None:
        advisor = PgBenchParameterAdvisor()
        hardware = HardwareSnapshot(
            collected_at="now",
            scope="host",
            platform="test",
            cpu_count=6,
            architecture="x86_64",
            memory_total_bytes=8 * 1024 ** 3,
            disk_total_bytes=None,
            disk_free_bytes=None,
        )

        recommendation = advisor.recommend(
            hardware,
            PgBenchConfig(enabled=True, duration_seconds=30, clients=4, jobs=1),
        )

        self.assertEqual(recommendation["recommended"]["duration_seconds"], 60)
        self.assertEqual(recommendation["recommended"]["jobs"], 6)
        self.assertEqual(recommendation["recommended"]["clients"], 24)
        self.assertIn("starting point", recommendation["summary"])

    def test_runner_uses_workload_script_and_parses_metrics(self) -> None:
        fake_runner = FakeRunner(
            stdout="latency average = 12.34 ms\ntps = 456.78 (without initial connection time)\n",
            stderr="progress: 1.0 s, 400.0 tps, lat 11.0 ms stddev 0.2\nprogress: 2.0 s, 450.0 tps, lat 10.0 ms stddev 0.2\n",
        )
        runner = PgBenchRunner(runner=fake_runner)
        config = PgBenchConfig(enabled=True, duration_seconds=30, clients=2, jobs=1)
        postgres = PostgresConfig(user="llm", password="secret", original_database="source_db")

        with tempfile.TemporaryDirectory() as tmpdir:
            workload = Path(tmpdir) / "workload.sql"
            workload.write_text("SELECT 1;", encoding="utf-8")
            result = runner.run(config, postgres, "source_db", workload_path=workload)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.average_latency_ms, 12.34)
        self.assertEqual(result.throughput_tps, 456.78)
        self.assertIn("-f", fake_runner.last_args)
        self.assertIn(str(workload), fake_runner.last_args)
        self.assertIn("-T", fake_runner.last_args)
        self.assertIn("30", fake_runner.last_args)
        self.assertIn("-P", fake_runner.last_args)
        self.assertEqual(result.progress_samples[1]["throughput_tps"], 450.0)
        self.assertEqual(fake_runner.last_env, {"PGPASSWORD": "secret"})

    def test_runner_skips_when_workload_is_missing(self) -> None:
        fake_runner = FakeRunner()
        runner = PgBenchRunner(runner=fake_runner)
        config = PgBenchConfig(enabled=True, duration_seconds=30)
        postgres = PostgresConfig()
        missing = Path("/tmp/definitely-missing-ilvesbench-workload.sql")

        result = runner.run(config, postgres, "source_db", workload_path=missing)

        self.assertEqual(result.status, "skipped")
        self.assertIn("was not found", result.stderr)

    def test_service_filters_invalid_rewritten_workload_before_pgbench(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workload = root / "rewritten.sql"
            workload.write_text("SELECT 1;\n\nSELECT bad_column FROM missing_table;\n", encoding="utf-8")
            service = BenchmarkerService(config=type("Config", (), {
                "postgres": type("Postgres", (), {"new_database": "target_db"})()
            })())

            def validate(statements: list[str]) -> list[dict]:
                return [{"index": 1, "statement": statements[1], "error": "column does not exist"}]

            def write_text_artifact(run_id: str, name: str, content: str) -> str:
                path = root / f"{name}.sql"
                path.write_text(content, encoding="utf-8")
                return str(path)

            filtered, warning = service.prepare_validated_workload(
                run_id="run-test",
                workload_path=workload,
                target_database="target_db",
                validate_statements=validate,
                write_text_artifact=write_text_artifact,
            )

            self.assertEqual(filtered.name, "rewritten_workload_db_new_validated.sql")
            self.assertIsNotNone(warning)
            self.assertEqual(warning["status"], "warning")
            self.assertEqual(warning["valid_statement_count"], 1)
            self.assertEqual(filtered.read_text(encoding="utf-8").strip(), "SELECT 1;")

    def test_service_runs_pgbench_through_configured_runner(self) -> None:
        fake_pgbench = FakePgBench()
        config = IlvesBenchConfig(postgres=PostgresConfig(original_database="source_db", new_database="target_db"))
        service = BenchmarkerService(config, pgbench=fake_pgbench)
        pgbench_config = PgBenchConfig(enabled=True, duration_seconds=30, clients=2, jobs=1)
        workload = Path("/tmp/workload.sql")

        result = service.run_pgbench(
            pgbench_config,
            config.postgres,
            "source_db",
            workload_path=workload,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(fake_pgbench.calls, [(pgbench_config, config.postgres, "source_db", workload)])

    def test_query_rewrite_batches_and_progress_payload(self) -> None:
        config = IlvesBenchConfig(postgres=PostgresConfig(original_database="source_db", new_database="target_db"))
        service = QueryRewriteService(config, batch_size=2)

        progress = service.progress_payload(
            "SELECT 1;",
            ["SELECT 1;", "SELECT 2;", "SELECT 3;"],
            [{"schema": "public", "name": "source_items"}],
            [{"name": "items"}],
            [{"sql": "INSERT INTO items SELECT * FROM public.items;"}],
        )
        batches = service.batches([(1, "a", "ka"), (2, "b", "kb"), (3, "c", "kc")])

        self.assertEqual(progress["total_query_count"], 3)
        self.assertEqual(progress["source_table_count"], 1)
        self.assertEqual(progress["target_table_count"], 1)
        self.assertEqual(progress["llm_request"]["batch_size"], 2)
        self.assertEqual([len(batch) for batch in batches], [2, 1])

    def test_query_rewrite_completes_only_valid_rewrites_in_source_order(self) -> None:
        config = IlvesBenchConfig(postgres=PostgresConfig(original_database="source_db", new_database="target_db"))
        service = QueryRewriteService(config, batch_size=10)
        progress = {
            "query_results": [
                {
                    "query_index": 2,
                    "rewritten_statement": "SELECT missing FROM target;",
                    "validation": {"status": "failed"},
                },
                {
                    "query_index": 1,
                    "rewritten_statement": "SELECT id FROM target;",
                    "validation": {"status": "passed"},
                },
                {
                    "query_index": 3,
                    "rewritten_statement": "SELECT name FROM target;",
                    "validation": {"status": "passed"},
                },
            ],
            "statements": [],
        }

        ordered_results, ordered_statements, invalid_count = service.complete_progress(progress)

        self.assertEqual([item["query_index"] for item in ordered_results], [1, 2, 3])
        self.assertEqual(ordered_statements, ["SELECT id FROM target;", "SELECT name FROM target;"])
        self.assertEqual(invalid_count, 1)
        self.assertEqual(progress["completed_query_count"], 2)
        self.assertEqual(progress["processed_query_count"], 3)

    def test_query_rewrite_cache_key_and_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = IlvesBenchConfig(
                postgres=PostgresConfig(
                    original_database="source_db",
                    new_database="target_db",
                    schemas=["public"],
                ),
                storage=StorageConfig(artifact_dir="artifacts"),
                config_path=str(Path(tmpdir) / "config.toml"),
            )
            service = QueryRewriteService(config)
            target_tables = [{"name": "items", "columns": [{"name": "id"}]}]
            source_tables = [{"schema": "public", "name": "source_items", "columns": [{"name": "id"}]}]

            first_key = service.cache_key("SELECT   id\nFROM items;", target_tables, source_tables=source_tables)
            second_key = service.cache_key("SELECT id FROM items;", target_tables, source_tables=source_tables)
            cache = {first_key: {"rewritten_statement": "SELECT id FROM items_new;"}}
            service.save_cache(cache)

            self.assertEqual(first_key, second_key)
            self.assertEqual(service.load_cache(), cache)
            self.assertEqual(service.cache_path().name, "query_rewrite_cache.json")

    def test_query_rewrite_rewrites_in_batches_and_saves_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = IlvesBenchConfig(
                postgres=PostgresConfig(original_database="source_db", new_database="target_db"),
                storage=StorageConfig(artifact_dir="artifacts"),
                config_path=str(Path(tmpdir) / "config.toml"),
            )
            service = QueryRewriteService(config, batch_size=2)
            rewrite_calls: list[str] = []
            progress_snapshots: list[dict] = []

            def rewrite_batch(
                sql: str,
                source_tables: list[dict],
                target_tables: list[dict],
                migration_statements: list[dict],
                batch_size: int,
            ) -> WorkloadRewriteProposal:
                rewrite_calls.append(sql)
                return WorkloadRewriteProposal(
                    status="planned",
                    summary="rewritten",
                    statements=[
                        statement.replace("source_items", "target_items")
                        for statement in sql.split(";")
                        if statement.strip()
                    ],
                    source="llm",
                    raw_response_text="ok",
                    request_payload={"source_statements": sql, "batch_size": batch_size},
                )

            result = service.rewrite_incremental(
                workload_sql="SELECT id FROM source_items; SELECT name FROM source_items; SELECT code FROM source_items;",
                source_statements=[
                    "SELECT id FROM source_items;",
                    "SELECT name FROM source_items;",
                    "SELECT code FROM source_items;",
                ],
                source_tables=[{"schema": "public", "name": "source_items", "columns": [{"name": "id"}]}],
                target_tables=[{"name": "target_items"}],
                migration_statements=[],
                rewrite_batch=rewrite_batch,
                validate_statement=lambda statement: {"status": "passed", "statement": statement},
                on_progress=lambda progress: progress_snapshots.append(dict(progress)),
            )

            self.assertEqual(len(rewrite_calls), 2)
            self.assertEqual(result.ordered_statements, [
                "SELECT id FROM target_items",
                "SELECT name FROM target_items",
                "SELECT code FROM target_items",
            ])
            self.assertEqual(result.invalid_count, 0)
            self.assertEqual(progress_snapshots[-1]["completed_query_count"], 3)
            self.assertEqual(len(service.load_cache()), 3)

    def test_query_rewrite_repairs_after_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = IlvesBenchConfig(
                postgres=PostgresConfig(original_database="source_db", new_database="target_db"),
                storage=StorageConfig(artifact_dir="artifacts"),
                config_path=str(Path(tmpdir) / "config.toml"),
            )
            service = QueryRewriteService(config, batch_size=5)
            validations: list[str] = []

            def rewrite_batch(
                sql: str,
                source_tables: list[dict],
                target_tables: list[dict],
                migration_statements: list[dict],
                batch_size: int,
            ) -> WorkloadRewriteProposal:
                return WorkloadRewriteProposal(
                    status="planned",
                    summary="bad rewrite",
                    statements=["SELECT missing FROM target_items;"],
                    source="llm",
                    raw_response_text="raw bad response",
                    request_payload={"source_statements": sql},
                )

            def repair_rewrite(
                source_statement: str,
                previous_rewrite: str,
                validation: dict,
                source_tables: list[dict],
                target_tables: list[dict],
                migration_statements: list[dict],
                query_index: int,
                batch_size: int,
            ) -> WorkloadRewriteProposal:
                return WorkloadRewriteProposal(
                    status="planned",
                    summary="repaired",
                    statements=["SELECT id FROM target_items;"],
                    source="llm",
                    raw_response_text="raw repaired response",
                    request_payload={"validation_error": validation, "previous_rewrite": previous_rewrite},
                )

            def validate(statement: str) -> dict:
                validations.append(statement)
                if "missing" in statement:
                    return {"status": "failed", "error": "column missing"}
                return {"status": "passed"}

            result = service.rewrite_incremental(
                workload_sql="SELECT id FROM source_items;",
                source_statements=["SELECT id FROM source_items;"],
                source_tables=[{"schema": "public", "name": "source_items", "columns": [{"name": "id"}]}],
                target_tables=[{"name": "target_items"}],
                migration_statements=[],
                rewrite_batch=rewrite_batch,
                repair_rewrite=repair_rewrite,
                validate_statement=validate,
                on_progress=lambda progress: None,
            )

            self.assertEqual(validations, ["SELECT missing FROM target_items;", "SELECT id FROM target_items;"])
            self.assertEqual(result.ordered_statements, ["SELECT id FROM target_items;"])
            self.assertEqual(result.ordered_results[0]["rewrite_attempt_count"], 2)
            self.assertIn("raw repaired response", service.raw_response_text(result.progress))

    def test_query_rewrite_raises_with_progress_when_validation_repair_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = IlvesBenchConfig(
                postgres=PostgresConfig(original_database="source_db", new_database="target_db"),
                storage=StorageConfig(artifact_dir="artifacts"),
                config_path=str(Path(tmpdir) / "config.toml"),
            )
            service = QueryRewriteService(config, batch_size=5)

            def rewrite_batch(
                sql: str,
                source_tables: list[dict],
                target_tables: list[dict],
                migration_statements: list[dict],
                batch_size: int,
            ) -> WorkloadRewriteProposal:
                return WorkloadRewriteProposal(
                    status="planned",
                    summary="bad rewrite",
                    statements=["SELECT missing FROM target_items;"],
                    source="llm",
                    raw_response_text="raw bad response",
                    request_payload={"source_statements": sql},
                )

            with self.assertRaises(QueryRewriteExecutionError) as ctx:
                service.rewrite_incremental(
                    workload_sql="SELECT id FROM source_items;",
                    source_statements=["SELECT id FROM source_items;"],
                    source_tables=[{"schema": "public", "name": "source_items", "columns": [{"name": "id"}]}],
                    target_tables=[{"name": "target_items"}],
                    migration_statements=[],
                    rewrite_batch=rewrite_batch,
                    validate_statement=lambda statement: {"status": "failed", "error": "column missing"},
                    on_progress=lambda progress: None,
                )

            self.assertEqual(ctx.exception.progress["processed_query_count"], 1)
            self.assertIn("no pgbench workload was generated", str(ctx.exception))
            self.assertIn("raw bad response", service.raw_response_text(ctx.exception.progress))


if __name__ == "__main__":
    unittest.main()
