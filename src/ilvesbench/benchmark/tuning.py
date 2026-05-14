from __future__ import annotations

from dataclasses import dataclass, field

from ilvesbench.config import PgBenchConfig
from ilvesbench.models import LogSummary


@dataclass(slots=True)
class KnobRecommendation:
    setting: str
    recommended_value: str
    reason: str
    risk: str = "review"


@dataclass(slots=True)
class TuningPlan:
    status: str
    summary: str
    recommendations: list[KnobRecommendation] = field(default_factory=list)
    source: str = "heuristic"


class PostgresTuningAdvisor:
    """Conservative PostgreSQL knob recommendations from hardware and workload snapshots."""

    def recommend(
        self,
        *,
        hardware: dict,
        log_summary: LogSummary | None,
        pgbench: PgBenchConfig,
    ) -> TuningPlan:
        memory_bytes = self._int_or_none(hardware.get("memory_total_bytes"))
        cpu_count = self._int_or_none(hardware.get("cpu_count")) or 1
        if memory_bytes is None or memory_bytes <= 0:
            return TuningPlan(
                status="insufficient_hardware",
                summary="Hardware memory was unavailable, so PostgreSQL knob recommendations were not generated.",
            )

        memory_mb = max(1, memory_bytes // (1024 * 1024))
        active_clients = max(1, pgbench.clients)
        query_count = len(log_summary.top_queries) if log_summary is not None else 0
        multi_statement_transactions = log_summary.multi_statement_transactions if log_summary is not None else 0

        shared_buffers_mb = self._clamp(memory_mb // 4, 128, 8192)
        effective_cache_size_mb = self._clamp((memory_mb * 3) // 4, 256, 65536)
        maintenance_work_mem_mb = self._clamp(memory_mb // 16, 64, 2048)
        work_mem_mb = self._clamp(memory_mb // max(active_clients * 16, 1), 4, 256)
        max_worker_processes = self._clamp(cpu_count * 2, 8, 64)
        max_parallel_workers = self._clamp(cpu_count, 2, 32)
        max_parallel_workers_per_gather = self._clamp(cpu_count // 2, 1, 8)

        recommendations = [
            KnobRecommendation(
                "shared_buffers",
                f"{shared_buffers_mb}MB",
                "About 25% of available memory is a conservative starting point for PostgreSQL buffer cache.",
            ),
            KnobRecommendation(
                "effective_cache_size",
                f"{effective_cache_size_mb}MB",
                "Planner hint set near 75% of memory so index plans reflect likely OS plus PostgreSQL cache capacity.",
            ),
            KnobRecommendation(
                "maintenance_work_mem",
                f"{maintenance_work_mem_mb}MB",
                "Improves CREATE INDEX and maintenance operations during schema experiments without overcommitting memory.",
            ),
            KnobRecommendation(
                "work_mem",
                f"{work_mem_mb}MB",
                f"Balances per-operation memory against the configured pgbench client count ({active_clients}).",
            ),
            KnobRecommendation(
                "max_worker_processes",
                str(max_worker_processes),
                "Keeps enough workers available for parallel query and maintenance on the detected CPU count.",
            ),
            KnobRecommendation(
                "max_parallel_workers",
                str(max_parallel_workers),
                "Allows PostgreSQL to use available cores for larger scans and joins.",
            ),
            KnobRecommendation(
                "max_parallel_workers_per_gather",
                str(max_parallel_workers_per_gather),
                "Caps per-query parallelism so one benchmark query does not monopolize all workers.",
            ),
        ]

        if query_count > 0:
            recommendations.append(
                KnobRecommendation(
                    "track_io_timing",
                    "on",
                    "Enables richer I/O timing metrics for benchmark comparisons.",
                    risk="low",
                )
            )

        if multi_statement_transactions > 0:
            recommendations.append(
                KnobRecommendation(
                    "checkpoint_timeout",
                    "15min",
                    "Multi-statement workloads can benefit from less frequent checkpoints during controlled benchmark runs.",
                )
            )

        return TuningPlan(
            status="recommended",
            summary=f"Generated {len(recommendations)} conservative PostgreSQL knob recommendation(s).",
            recommendations=recommendations,
        )

    def _int_or_none(self, value) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _clamp(self, value: int, lower: int, upper: int) -> int:
        return max(lower, min(upper, value))
