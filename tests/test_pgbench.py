from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.benchmark.pgbench import PgBenchRunner
from ilvesbench.config import PgBenchConfig, PostgresConfig


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


class PgBenchTests(unittest.TestCase):
    def test_runner_uses_workload_script_and_parses_metrics(self) -> None:
        fake_runner = FakeRunner(
            stdout="latency average = 12.34 ms\ntps = 456.78 (without initial connection time)\n",
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


if __name__ == "__main__":
    unittest.main()
