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


if __name__ == "__main__":
    unittest.main()
