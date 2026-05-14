from dataclasses import asdict
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.benchmark.energy import BenchmarkComparator, EnergyEstimator
from ilvesbench.benchmark.index_advisor import IndexAdvisor
from ilvesbench.benchmark.tuning import PostgresTuningAdvisor
from ilvesbench.config import EnergyConfig, PgBenchConfig
from ilvesbench.models import BenchmarkMetrics, LogSummary, QueryObservation


class BenchmarkAdvisorTests(unittest.TestCase):
    def test_index_advisor_recommends_workload_predicate_indexes_for_target_tables(self) -> None:
        advisor = IndexAdvisor()
        plan = advisor.recommend(
            schema=None,
            target_tables=[
                {
                    "name": "items_lookup",
                    "columns": [
                        {"name": "id"},
                        {"name": "name"},
                    ],
                    "primary_key": ["id"],
                }
            ],
            workload_sql="SELECT id FROM items_lookup WHERE name = 'alpha' ORDER BY name;",
        )

        self.assertEqual(plan.status, "recommended")
        self.assertEqual(len(plan.recommendations), 1)
        recommendation = plan.recommendations[0]
        self.assertEqual(recommendation.table, "items_lookup")
        self.assertEqual(recommendation.columns, ["name"])
        self.assertIn("CREATE INDEX IF NOT EXISTS", recommendation.sql)

    def test_tuning_advisor_generates_memory_and_parallelism_knobs(self) -> None:
        advisor = PostgresTuningAdvisor()
        plan = advisor.recommend(
            hardware={"memory_total_bytes": 8 * 1024 * 1024 * 1024, "cpu_count": 4},
            log_summary=LogSummary(
                path="workload.sql",
                lines_processed=1,
                statements_detected=1,
                transactions_detected=0,
                multi_statement_transactions=0,
                top_queries=[
                    QueryObservation(
                        fingerprint="select * from items where name = ?",
                        sample_sql="SELECT * FROM items WHERE name = 'alpha'",
                        count=1,
                    )
                ],
                sampled_transactions=[],
                source_kind="workload_file",
            ),
            pgbench=PgBenchConfig(enabled=True, clients=4),
        )

        recommendations = {item.setting: item.recommended_value for item in plan.recommendations}
        self.assertEqual(plan.status, "recommended")
        self.assertEqual(recommendations["shared_buffers"], "2048MB")
        self.assertEqual(recommendations["track_io_timing"], "on")
        self.assertIn("max_parallel_workers", recommendations)

    def test_energy_estimator_and_comparator_report_before_after_changes(self) -> None:
        estimator = EnergyEstimator()
        original = BenchmarkMetrics(
            database="db_original",
            status="completed",
            command=["pgbench"],
            duration_seconds=30,
            clients=4,
            jobs=1,
            transactions=None,
            throughput_tps=100.0,
            average_latency_ms=20.0,
        )
        normalized = BenchmarkMetrics(
            database="db_new",
            status="completed",
            command=["pgbench"],
            duration_seconds=30,
            clients=4,
            jobs=1,
            transactions=None,
            throughput_tps=125.0,
            average_latency_ms=16.0,
        )
        energy_config = EnergyConfig(enabled=True, estimated_cpu_watts=60.0)
        original_energy = estimator.estimate(original, {}, energy_config)
        normalized_energy = estimator.estimate(normalized, {}, energy_config)

        comparison = BenchmarkComparator().compare(
            original=asdict(original),
            normalized=asdict(normalized),
            original_energy=asdict(original_energy),
            normalized_energy=asdict(normalized_energy),
        )

        self.assertEqual(comparison["status"], "completed")
        self.assertEqual(comparison["throughput_change_percent"], 25.0)
        self.assertEqual(comparison["latency_improvement_percent"], 20.0)
        self.assertLess(comparison["energy_per_transaction_change_percent"], 0)


if __name__ == "__main__":
    unittest.main()
