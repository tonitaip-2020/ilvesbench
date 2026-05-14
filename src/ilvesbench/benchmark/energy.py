from __future__ import annotations

from dataclasses import dataclass

from ilvesbench.config import EnergyConfig
from ilvesbench.models import BenchmarkMetrics


@dataclass(slots=True)
class EnergyEstimate:
    status: str
    source: str
    duration_seconds: int
    estimated_watts: float | None = None
    energy_joules: float | None = None
    energy_kwh: float | None = None
    estimated_co2_grams: float | None = None
    estimated_transactions: float | None = None
    joules_per_transaction: float | None = None
    summary: str = ""


class EnergyEstimator:
    """Energy-aware benchmark estimates from pgbench runtime and configured wattage."""

    def estimate(
        self,
        benchmark: BenchmarkMetrics,
        hardware: dict,
        config: EnergyConfig,
    ) -> EnergyEstimate:
        if not config.enabled:
            return EnergyEstimate(
                status="disabled",
                source="disabled",
                duration_seconds=benchmark.duration_seconds,
                summary="Energy estimation is disabled in the config.",
            )

        if benchmark.status != "completed":
            return EnergyEstimate(
                status="skipped",
                source="benchmark_not_completed",
                duration_seconds=benchmark.duration_seconds,
                summary="Energy estimation requires a completed pgbench run.",
            )

        watts = config.estimated_cpu_watts
        source = "config.estimated_cpu_watts"
        if watts is None or watts <= 0:
            cpu_count = self._int_or_none(hardware.get("cpu_count")) or 1
            watts = max(15.0, cpu_count * config.estimated_watts_per_cpu)
            source = "cpu_count_estimate"

        duration_seconds = max(0, benchmark.duration_seconds)
        energy_joules = watts * duration_seconds
        energy_kwh = energy_joules / 3_600_000
        estimated_transactions = None
        joules_per_transaction = None
        if benchmark.throughput_tps is not None:
            estimated_transactions = benchmark.throughput_tps * duration_seconds
            if estimated_transactions > 0:
                joules_per_transaction = energy_joules / estimated_transactions

        co2_grams = energy_kwh * config.co2_grams_per_kwh
        return EnergyEstimate(
            status="estimated",
            source=source,
            duration_seconds=duration_seconds,
            estimated_watts=round(watts, 3),
            energy_joules=round(energy_joules, 3),
            energy_kwh=round(energy_kwh, 8),
            estimated_co2_grams=round(co2_grams, 6),
            estimated_transactions=round(estimated_transactions, 3) if estimated_transactions is not None else None,
            joules_per_transaction=round(joules_per_transaction, 6) if joules_per_transaction is not None else None,
            summary=(
                f"Estimated {round(energy_joules, 2)} J over {duration_seconds}s "
                f"using {round(watts, 2)} W ({source})."
            ),
        )

    def _int_or_none(self, value) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None


class BenchmarkComparator:
    """Compare original and normalized/re-written pgbench results."""

    def compare(
        self,
        *,
        original: dict | None,
        normalized: dict | None,
        original_energy: dict | None = None,
        normalized_energy: dict | None = None,
        storage: dict | None = None,
    ) -> dict:
        if not original or not normalized:
            return {
                "status": "awaiting_benchmarks",
                "summary": "Run pgbench for both db-original and db-new before comparison.",
            }

        if original.get("status") != "completed" or normalized.get("status") != "completed":
            return {
                "status": "incomplete",
                "summary": "Both pgbench runs must complete successfully before comparison.",
                "original_status": original.get("status"),
                "normalized_status": normalized.get("status"),
            }

        original_tps = self._float_or_none(original.get("throughput_tps"))
        normalized_tps = self._float_or_none(normalized.get("throughput_tps"))
        original_latency = self._float_or_none(original.get("average_latency_ms"))
        normalized_latency = self._float_or_none(normalized.get("average_latency_ms"))

        result = {
            "status": "completed",
            "summary": "Compared db-original workload against db-new normalized rewritten workload.",
            "original_database": original.get("database"),
            "normalized_database": normalized.get("database"),
            "original_tps": original_tps,
            "normalized_tps": normalized_tps,
            "original_average_latency_ms": original_latency,
            "normalized_average_latency_ms": normalized_latency,
        }

        if original_tps and normalized_tps:
            result["throughput_delta_tps"] = round(normalized_tps - original_tps, 6)
            result["throughput_change_percent"] = round(((normalized_tps - original_tps) / original_tps) * 100, 3)
            result["throughput_ratio"] = round(normalized_tps / original_tps, 6)

        if original_latency and normalized_latency:
            result["latency_delta_ms"] = round(normalized_latency - original_latency, 6)
            result["latency_change_percent"] = round(((normalized_latency - original_latency) / original_latency) * 100, 3)
            result["latency_improvement_percent"] = round(((original_latency - normalized_latency) / original_latency) * 100, 3)

        original_jpt = self._float_or_none((original_energy or {}).get("joules_per_transaction"))
        normalized_jpt = self._float_or_none((normalized_energy or {}).get("joules_per_transaction"))
        if original_jpt and normalized_jpt:
            result["energy_per_transaction_delta_joules"] = round(normalized_jpt - original_jpt, 6)
            result["energy_per_transaction_change_percent"] = round(((normalized_jpt - original_jpt) / original_jpt) * 100, 3)

        if storage:
            result["storage"] = storage

        return result

    def _float_or_none(self, value) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
