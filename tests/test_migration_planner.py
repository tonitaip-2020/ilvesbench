from pathlib import Path
import json
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.benchmark.migration_planner import MigrationPlanner
from ilvesbench.models import LLMResult, SchemaSnapshot


class StaticGateway:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def generate(self, messages, model=None, max_tokens=256) -> LLMResult:
        return LLMResult(
            backend="aviary",
            model=model or "fake-model",
            response_text=json.dumps(self._payload),
            raw_response={},
        )


class MigrationPlannerTests(unittest.TestCase):
    def test_generates_deterministic_1nf_split_migration(self) -> None:
        proposal = MigrationPlanner().plan(
            schema=None,
            target_tables=[
                {
                    "name": "title_crew",
                    "source_tables": ["public.title_crew"],
                    "columns": [
                        {"source_column": "tconst", "name": "tconst"},
                    ],
                    "migration_strategy": "copy_distinct",
                },
                {
                    "name": "title_crew_directors",
                    "source_tables": ["public.title_crew"],
                    "columns": [
                        {"source_column": "tconst", "name": "tconst"},
                        {"source_column": "directors", "name": "nconst"},
                    ],
                    "migration_strategy": "split_delimited",
                    "split_source_column": "directors",
                    "split_value_column": "nconst",
                    "split_delimiter": ",",
                },
            ],
        )

        self.assertEqual(proposal.source, "deterministic_1nf")
        self.assertEqual(len(proposal.statements), 2)
        self.assertIn('string_to_array("directors", \',\')', proposal.statements[1]["sql"])
        self.assertIn("trim(extracted_value)", proposal.statements[1]["sql"])

    def test_deterministic_1nf_migration_quotes_mixed_case_source_columns(self) -> None:
        proposal = MigrationPlanner().plan(
            schema=None,
            target_tables=[
                {
                    "name": "title_basics",
                    "source_tables": ["public.title_basics"],
                    "columns": [
                        {"source_column": "tconst", "name": "tconst"},
                        {"source_column": "titleSearchCol", "name": "titlesearchcol"},
                    ],
                    "migration_strategy": "copy_distinct",
                },
            ],
        )

        self.assertEqual(proposal.source, "deterministic_1nf")
        self.assertIn('"titleSearchCol"', proposal.statements[0]["sql"])
        self.assertIn('"titlesearchcol"', proposal.statements[0]["sql"])

    def test_normalizes_source_schema_reference_shape(self) -> None:
        planner = MigrationPlanner(
            llm=StaticGateway(
                {
                    "status": "planned",
                    "summary": "ok",
                    "reasoning": [],
                    "statements": [
                        {
                            "target_table": "students",
                            "purpose": "load students",
                            "sql": "INSERT INTO students (id, name) SELECT DISTINCT student_id, student_name FROM __SOURCE_SCHEMA__.public.studentcourses",
                        }
                    ],
                }
            )
        )
        target_tables = [
            {
                "name": "students",
                "columns": [
                    {"name": "id"},
                    {"name": "name"},
                ],
            }
        ]

        proposal = planner.plan(
            SchemaSnapshot(database="demo", collected_at="2026-04-23T00:00:00+00:00", tables=[]),
            target_tables,
        )

        self.assertEqual(
            proposal.statements[0]["sql"],
            "INSERT INTO students (id, name) SELECT DISTINCT student_id, student_name FROM __SOURCE_SCHEMA__.studentcourses;",
        )

    def test_rejects_insert_columns_not_in_target_table(self) -> None:
        planner = MigrationPlanner(
            llm=StaticGateway(
                {
                    "status": "planned",
                    "summary": "bad",
                    "reasoning": [],
                    "statements": [
                        {
                            "target_table": "students",
                            "purpose": "load students",
                            "sql": "INSERT INTO students (student_id, student_name) SELECT student_id, student_name FROM __SOURCE_SCHEMA__.studentcourses",
                        }
                    ],
                }
            )
        )
        target_tables = [
            {
                "name": "students",
                "columns": [
                    {"name": "id"},
                    {"name": "name"},
                ],
            }
        ]

        with self.assertRaises(ValueError) as ctx:
            planner.plan(
                SchemaSnapshot(database="demo", collected_at="2026-04-23T00:00:00+00:00", tables=[]),
                target_tables,
            )

        self.assertIn("uses columns not present", str(ctx.exception))


    def test_generates_deterministic_array_unnest_migration(self) -> None:
        proposal = MigrationPlanner().plan(
            schema=None,
            target_tables=[
                {
                    "name": "film",
                    "source_tables": ["public.film"],
                    "columns": [{"source_column": "film_id", "name": "film_id"}],
                    "migration_strategy": "copy_distinct",
                },
                {
                    "name": "film_special_features",
                    "source_tables": ["public.film"],
                    "columns": [
                        {"source_column": "film_id", "name": "film_id"},
                        {"source_column": "special_features", "name": "special_feature"},
                    ],
                    "migration_strategy": "unnest_array",
                    "split_source_column": "special_features",
                    "split_value_column": "special_feature",
                },
            ],
        )

        self.assertEqual(proposal.source, "deterministic_1nf")
        self.assertIn('unnest("special_features")', proposal.statements[1]["sql"])
        self.assertNotIn("string_to_array", proposal.statements[1]["sql"])


if __name__ == "__main__":
    unittest.main()
