from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.benchmark.workload import WorkloadPlanner
from ilvesbench.models import LLMResult, LogSummary, QueryObservation


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


class WorkloadPlannerTests(unittest.TestCase):
    def test_rewrite_returns_select_statements(self) -> None:
        planner = WorkloadPlanner(
            llm=StaticGateway(
                {
                    "status": "planned",
                    "summary": "Rewritten.",
                    "reasoning": [],
                    "statements": [
                        "SELECT s.id, s.name FROM students s WHERE s.id = 1",
                        "SELECT c.name FROM courses c WHERE c.id = 1",
                    ],
                }
            )
        )

        proposal = planner.rewrite(
            "SELECT student_id, student_name FROM studentcourses WHERE student_id = 1;",
            target_tables=[{"name": "students"}, {"name": "courses"}],
            migration_statements=[],
        )

        self.assertEqual(proposal.status, "planned")
        self.assertEqual(len(proposal.statements), 2)
        self.assertTrue(all(statement.endswith(";") for statement in proposal.statements))

    def test_rewrite_rejects_ddl_output(self) -> None:
        planner = WorkloadPlanner(
            llm=StaticGateway(
                {
                    "status": "planned",
                    "summary": "Bad rewrite.",
                    "reasoning": [],
                    "statements": [
                        "CREATE TABLE bad_table (id integer)",
                    ],
                }
            )
        )

        with self.assertRaises(ValueError) as ctx:
            planner.rewrite(
                "SELECT student_id FROM studentcourses;",
                target_tables=[{"name": "students"}],
                migration_statements=[],
            )

        self.assertIn("SELECT, INSERT, UPDATE, or DELETE", str(ctx.exception))

    def test_rewrite_strips_database_qualifiers_for_target_tables(self) -> None:
        planner = WorkloadPlanner(
            llm=StaticGateway(
                {
                    "status": "planned",
                    "summary": "Rewritten.",
                    "reasoning": [],
                    "statements": [
                        "SELECT s.student_id FROM db-new.students s WHERE s.student_id = 1",
                        "SELECT c.course_name FROM mvp_db_new.courses c WHERE c.course_code = 'CS101'",
                    ],
                }
            ),
            target_database="mvp_db_new",
        )

        proposal = planner.rewrite(
            "SELECT student_id FROM studentcourses WHERE student_id = 1;",
            target_tables=[{"name": "students"}, {"name": "courses"}],
            migration_statements=[],
        )

        self.assertEqual(
            proposal.statements[0],
            "SELECT s.student_id FROM students s WHERE s.student_id = 1;",
        )
        self.assertEqual(
            proposal.statements[1],
            "SELECT c.course_name FROM courses c WHERE c.course_code = 'CS101';",
        )

    def test_build_plan_recommends_summary_table_candidates_for_aggregate_workload(self) -> None:
        planner = WorkloadPlanner()
        plan = planner.build_plan(
            LogSummary(
                path="workload.sql",
                lines_processed=1,
                statements_detected=1,
                transactions_detected=0,
                multi_statement_transactions=0,
                top_queries=[
                    QueryObservation(
                        fingerprint="orders by day",
                        sample_sql="SELECT customer_id, count(*) FROM orders GROUP BY customer_id;",
                        count=42,
                    )
                ],
                sampled_transactions=[],
                source_kind="workload_file",
            )
        )

        self.assertEqual(plan.status, "recommended")
        self.assertEqual(len(plan.candidate_summary_tables), 1)
        self.assertEqual(plan.candidate_summary_tables[0]["pattern"], "aggregate_group_by")


if __name__ == "__main__":
    unittest.main()
