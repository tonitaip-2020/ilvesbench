from __future__ import annotations

from pathlib import Path
import re

from ilvesbench.config import PgBenchConfig, PostgresConfig
from ilvesbench.models import BenchmarkMetrics, HardwareSnapshot
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


class PgBenchParameterAdvisor:
    def recommend(self, hardware: HardwareSnapshot | None, current: PgBenchConfig) -> dict:
        cpu_count = int(hardware.cpu_count or 1) if hardware is not None else 1
        memory_bytes = int(hardware.memory_total_bytes or 0) if hardware is not None and hardware.memory_total_bytes else 0
        jobs = max(1, min(cpu_count, 8))
        clients = max(4, jobs * 4)
        if memory_bytes:
            memory_gib = memory_bytes / (1024 ** 3)
            clients = min(clients, max(4, int(memory_gib * 8)))
        duration_seconds = max(int(current.duration_seconds or 0), 60)
        recommendation = {
            "enabled": current.enabled,
            "command": current.command,
            "duration_seconds": duration_seconds,
            "clients": clients,
            "jobs": jobs,
            "transactions": current.transactions,
            "source": "hardware_rule_of_thumb",
            "hardware": {
                "scope": hardware.scope if hardware else "unknown",
                "cpu_count": cpu_count,
                "memory_total_bytes": memory_bytes or None,
                "container_name": hardware.container_name if hardware else None,
            },
            "rationale": [
                f"Use up to {jobs} pgbench worker job(s), bounded by detected CPU count and a conservative cap.",
                f"Start with {clients} client(s), roughly four clients per job unless memory limits are tight.",
                "Use at least 60 seconds for benchmark duration so short startup effects matter less.",
            ],
        }
        return {
            "status": "recommended",
            "summary": (
                f"Recommended pgbench -T {duration_seconds} -c {clients} -j {jobs} "
                "from detected hardware. Treat this as a starting point, not a capacity proof."
            ),
            "current": {
                "enabled": current.enabled,
                "command": current.command,
                "duration_seconds": current.duration_seconds,
                "clients": current.clients,
                "jobs": current.jobs,
                "transactions": current.transactions,
            },
            "recommended": recommendation,
        }
