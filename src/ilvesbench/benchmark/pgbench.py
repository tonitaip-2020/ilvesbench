from __future__ import annotations

from pathlib import Path
import re

from ilvesbench.config import PgBenchConfig, PostgresConfig
from ilvesbench.models import BenchmarkMetrics
from ilvesbench.osops.subprocesses import SubprocessRunner


TPS_RE = re.compile(r"tps = (?P<tps>[0-9.]+)")
LATENCY_RE = re.compile(r"latency average = (?P<latency>[0-9.]+) ms")


class PgBenchRunner:
    def __init__(self, runner: SubprocessRunner | None = None) -> None:
        self._runner = runner or SubprocessRunner()

    def run(
        self,
        config: PgBenchConfig,
        postgres: PostgresConfig,
        database: str,
        workload_path: Path | None = None,
    ) -> BenchmarkMetrics:
        command = self._build_command(config, postgres, database, workload_path)
        if not config.enabled:
            return BenchmarkMetrics(
                database=database,
                status="skipped",
                command=command,
                duration_seconds=config.duration_seconds,
                clients=config.clients,
                jobs=config.jobs,
                transactions=config.transactions,
                stderr="pgbench is disabled in the config.",
            )

        if workload_path is None:
            return BenchmarkMetrics(
                database=database,
                status="skipped",
                command=command,
                duration_seconds=config.duration_seconds,
                clients=config.clients,
                jobs=config.jobs,
                transactions=config.transactions,
                stderr="No workload SQL file was configured for pgbench.",
            )

        if not workload_path.exists():
            return BenchmarkMetrics(
                database=database,
                status="skipped",
                command=command,
                duration_seconds=config.duration_seconds,
                clients=config.clients,
                jobs=config.jobs,
                transactions=config.transactions,
                stderr=f"Configured workload SQL file was not found: {workload_path}",
            )

        if self._runner.which(config.command) is None:
            return BenchmarkMetrics(
                database=database,
                status="skipped",
                command=command,
                duration_seconds=config.duration_seconds,
                clients=config.clients,
                jobs=config.jobs,
                transactions=config.transactions,
                stderr="pgbench executable was not found on PATH.",
            )

        result = self._runner.run(
            command,
            timeout=config.duration_seconds + 15,
            env={"PGPASSWORD": postgres.password},
        )
        output = result.stdout + "\n" + result.stderr
        tps_match = TPS_RE.search(output)
        latency_match = LATENCY_RE.search(output)
        return BenchmarkMetrics(
            database=database,
            status="completed" if result.returncode == 0 else "failed",
            command=command,
            duration_seconds=config.duration_seconds,
            clients=config.clients,
            jobs=config.jobs,
            transactions=config.transactions,
            throughput_tps=float(tps_match.group("tps")) if tps_match else None,
            average_latency_ms=float(latency_match.group("latency")) if latency_match else None,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _build_command(
        self,
        config: PgBenchConfig,
        postgres: PostgresConfig,
        database: str,
        workload_path: Path | None,
    ) -> list[str]:
        args = [
            config.command,
            "-h",
            postgres.host,
            "-p",
            str(postgres.port),
            "-U",
            postgres.user,
            "-d",
            database,
            "-c",
            str(config.clients),
            "-j",
            str(config.jobs),
            "-n",
        ]
        if workload_path is not None:
            args.extend(["-f", str(workload_path)])
        if config.transactions is not None:
            args.extend(["-t", str(config.transactions)])
        else:
            args.extend(["-T", str(config.duration_seconds)])
        return args
