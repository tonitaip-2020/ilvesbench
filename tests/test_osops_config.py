from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.config import IlvesBenchConfig, LogConfig, PostgresConfConfig, StorageConfig, WorkloadConfig
from ilvesbench.osops.service import OSOpsService


class OSOpsConfigTests(unittest.TestCase):
    def test_postgresql_conf_can_be_loaded_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = IlvesBenchConfig(
                postgresql_conf=PostgresConfConfig(path="postgresql.conf"),
                config_path=str(root / "config.toml"),
            )
            service = OSOpsService(config)

            saved = service.save_postgresql_conf("shared_buffers = '1GB'\n")
            loaded = service.postgresql_conf_status()

        self.assertEqual(saved["status"], "ok")
        self.assertEqual(loaded["status"], "ok")
        self.assertEqual(loaded["content"], "shared_buffers = '1GB'\n")

    def test_workload_source_context_prefers_configured_workload_file_when_log_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workload_path = root / "queries.sql"
            workload_path.write_text("SELECT 1;", encoding="utf-8")
            config = IlvesBenchConfig(
                logs=LogConfig(path="missing.log"),
                workload=WorkloadConfig(path="queries.sql"),
                config_path=str(root / "config.toml"),
            )
            service = OSOpsService(config)

            status = service.workload_source_status()
            summary = service.load_workload_source()

        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["selected_kind"], "workload_file")
        self.assertEqual(Path(status["resolved_workload_path"]), workload_path.resolve())
        self.assertIsNotNone(summary)
        self.assertEqual(summary.source_kind, "workload_file")
        self.assertEqual(summary.statements_detected, 1)

    def test_workload_source_loader_emits_pgbench_workloads_for_postgres_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_path = root / "postgresql.log"
            log_path.write_text(
                "2026-04-23 10:00:00.010 UTC [100] LOG:  duration: 1.234 ms  "
                "statement: SELECT * FROM users WHERE id = 1;",
                encoding="utf-8",
            )
            config = IlvesBenchConfig(
                logs=LogConfig(path="postgresql.log"),
                storage=StorageConfig(artifact_dir="artifacts"),
                config_path=str(root / "config.toml"),
            )
            service = OSOpsService(config)
            output_dir = root / "out"

            summary = service.load_workload_source(output_dir=output_dir)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.source_kind, "postgres_log")
            self.assertEqual(summary.statements_detected, 1)
            self.assertIn("unknown", summary.workload_outputs)
            self.assertTrue(Path(summary.workload_outputs["unknown"]["combined_workload_path"]).exists())


if __name__ == "__main__":
    unittest.main()
