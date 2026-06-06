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
from ilvesbench.models import (
    BenchmarkMetrics,
    ColumnMetadata,
    IndexMetadata,
    LLMResult,
    LogSummary,
    QueryObservation,
    SchemaSnapshot,
    TableMetadata,
    UniqueConstraintMetadata,
)


class StaticGateway:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.messages = []

    def generate(self, messages, model=None, max_tokens=256) -> LLMResult:
        self.messages.append(messages)
        payload = self._responses.pop(0) if self._responses else {"status": "no_recommendations", "recommendations": []}
        import json
        return LLMResult(
            backend="fake",
            model=model or "fake-model",
            response_text=json.dumps(payload),
            raw_response={},
        )


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

    def test_index_advisor_accepts_llm_compound_expression_and_drop_recommendations(self) -> None:
        gateway = StaticGateway([
            {
                "status": "recommended",
                "summary": "compound and expression indexes",
                "recommendations": [
                    {
                        "action": "create",
                        "table": "items",
                        "columns": ["category", "created_at"],
                        "name": "idx_items_category_created_at",
                        "sql": 'CREATE INDEX IF NOT EXISTS "idx_items_category_created_at" ON "items" ("category", "created_at" DESC);',
                        "reason": "supports category filter plus ordering",
                        "confidence": 0.86,
                        "index_type": "btree",
                    },
                    {
                        "action": "create",
                        "table": "items",
                        "columns": [],
                        "expressions": ["lower(name)"],
                        "name": "idx_items_lower_name",
                        "sql": 'CREATE INDEX IF NOT EXISTS "idx_items_lower_name" ON "items" (lower("name"));',
                        "reason": "supports case-insensitive lookup",
                        "confidence": 0.82,
                        "index_type": "btree",
                    },
                    {
                        "action": "drop",
                        "table": "items",
                        "columns": ["old_col"],
                        "name": "idx_items_old_col",
                        "sql": 'DROP INDEX IF EXISTS "idx_items_old_col";',
                        "reason": "redundant with workload-aware compound index",
                        "confidence": 0.71,
                    },
                ],
            }
        ])
        advisor = IndexAdvisor(llm=gateway)
        schema = SchemaSnapshot(
            database="target_db",
            collected_at="2026-06-05T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="items",
                    columns=[
                        ColumnMetadata(name="id", data_type="integer", is_nullable=False),
                        ColumnMetadata(name="category", data_type="text", is_nullable=True),
                        ColumnMetadata(name="created_at", data_type="timestamp", is_nullable=True),
                        ColumnMetadata(name="name", data_type="text", is_nullable=True),
                        ColumnMetadata(name="old_col", data_type="text", is_nullable=True),
                    ],
                    indexes=[
                        IndexMetadata(
                            name="idx_items_old_col",
                            definition='CREATE INDEX idx_items_old_col ON public.items USING btree (old_col)',
                            is_unique=False,
                        )
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="items_pkey", columns=["id"]),
                    ],
                )
            ],
        )

        plan = advisor.recommend(
            schema=schema,
            target_tables=[],
            workload_sql="SELECT * FROM items WHERE category = 'movie' ORDER BY created_at DESC; SELECT * FROM items WHERE lower(name) = 'x';",
            use_llm=True,
        )

        self.assertEqual(plan.status, "recommended")
        self.assertEqual(plan.source, "llm")
        self.assertEqual(len(plan.recommendations), 3)
        self.assertEqual(plan.recommendations[0].columns, ["category", "created_at"])
        self.assertEqual(plan.recommendations[1].expressions, ["lower(name)"])
        self.assertEqual(plan.recommendations[2].action, "drop")

    def test_index_advisor_batches_queries_and_carries_previous_recommendations(self) -> None:
        gateway = StaticGateway([
            {
                "status": "recommended",
                "recommendations": [
                    {
                        "action": "create",
                        "table": "items",
                        "columns": ["category"],
                        "name": "idx_items_category",
                        "sql": 'CREATE INDEX IF NOT EXISTS "idx_items_category" ON "items" ("category");',
                        "reason": "batch one",
                        "confidence": 0.7,
                    }
                ],
            },
            {
                "status": "recommended",
                "recommendations": [
                    {
                        "action": "create",
                        "table": "items",
                        "columns": ["created_at"],
                        "name": "idx_items_created_at",
                        "sql": 'CREATE INDEX IF NOT EXISTS "idx_items_created_at" ON "items" ("created_at");',
                        "reason": "batch two",
                        "confidence": 0.7,
                    }
                ],
            },
        ])
        advisor = IndexAdvisor(llm=gateway, query_batch_size=1)

        plan = advisor.recommend(
            schema=None,
            target_tables=[
                {
                    "name": "items",
                    "columns": [{"name": "category"}, {"name": "created_at"}],
                    "primary_key": [],
                }
            ],
            workload_sql="SELECT * FROM items WHERE category = 'x'; SELECT * FROM items ORDER BY created_at;",
            use_llm=True,
        )

        self.assertEqual(len(gateway.messages), 2)
        second_prompt = gateway.messages[1][1]["content"]
        self.assertIn("previous_recommendations", second_prompt)
        self.assertIn("idx_items_category", second_prompt)
        self.assertEqual(len(plan.recommendations), 2)

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
