from __future__ import annotations

from pathlib import Path

from ilvesbench.config import PgBenchConfig, PostgresConfig

from .aggregator import WorkloadAggregator
from .generator import PgBenchWorkloadEmitter
from .log_records import CompositeLogRecordParser
from .normalizer import PglastSQLNormalizer


class WorkloadLogPipeline:
    def __init__(
        self,
        *,
        parser: CompositeLogRecordParser | None = None,
        aggregator: WorkloadAggregator | None = None,
        emitter: PgBenchWorkloadEmitter | None = None,
    ) -> None:
        self._parser = parser or CompositeLogRecordParser()
        self._aggregator = aggregator or WorkloadAggregator(PglastSQLNormalizer())
        self._emitter = emitter or PgBenchWorkloadEmitter()

    @property
    def aggregator(self) -> WorkloadAggregator:
        return self._aggregator

    def process_files(
        self,
        paths: list[str | Path],
        *,
        output_dir: str | Path | None = None,
        max_lines: int | None = None,
        postgres: PostgresConfig | None = None,
        pgbench: PgBenchConfig | None = None,
    ) -> dict:
        lines_processed = 0
        for path in paths:
            target = Path(path).expanduser().resolve()
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if max_lines is not None and lines_processed >= max_lines:
                        break
                    lines_processed += 1
                    observation = self._parser.parse_line(
                        line,
                        source_file=str(target),
                        source_line=line_number,
                    )
                    if observation is not None:
                        self._aggregator.observe(observation)
                if max_lines is not None and lines_processed >= max_lines:
                    break

        workloads = self._aggregator.entries_by_database()
        outputs = {}
        if output_dir is not None:
            outputs = self._emitter.emit(
                output_dir,
                workloads,
                host=(postgres.host if postgres else "HOST"),
                port=(postgres.port if postgres else "PORT"),
                user=(postgres.user if postgres else "USER"),
                duration_seconds=(pgbench.duration_seconds if pgbench else 60),
                clients=(pgbench.clients if pgbench else 16),
                jobs=(pgbench.jobs if pgbench else 4),
            )
        return {
            "lines_processed": lines_processed,
            "observed_queries": self._aggregator.observed_count,
            "database_count": len(workloads),
            "workloads": workloads,
            "outputs": outputs,
            "skipped": self._aggregator.skipped,
            "skipped_summary": self._aggregator.skipped_summary(),
        }
