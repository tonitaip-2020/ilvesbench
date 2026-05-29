from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.benchmark.workload import WorkloadPlanner, WorkloadRewriteError
from ilvesbench.models import LLMResult, LogSummary, QueryObservation


class StaticGateway:
    def __init__(self, payload: dict | list[str]) -> None:
        self._payload = payload
        self.messages = []

    def generate(self, messages, model=None, max_tokens=256) -> LLMResult:
        self.messages.append(messages)
        if isinstance(self._payload, list):
            response_text = self._payload.pop(0)
        else:
            response_text = json.dumps(self._payload)
        return LLMResult(
            backend="aviary",
            model=model or "fake-model",
            response_text=response_text,
            raw_response={},
        )


class TimeoutGateway:
    def generate(self, messages, model=None, max_tokens=256) -> LLMResult:
        raise TimeoutError("The read operation timed out")


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

    def test_rewrite_strips_comments_and_pgbench_meta_commands_from_source_workload(self) -> None:
        planner = WorkloadPlanner()

        proposal = planner.rewrite(
            """
            -- Q1
            -- public baseline
            \\set nconst random(1, 100)
            SELECT tb.primarytitle
            FROM public.title_basics tb
            WHERE tb.titletype = 'movie';

            /*
             Q2: CTE query
            */
            WITH kft AS (
              SELECT UNNEST(STRING_TO_ARRAY(nb.knownfortitles, ',')) AS title
              FROM public.name_basics nb
            )
            SELECT tb.primarytitle
            FROM public.title_basics tb
            INNER JOIN kft ON kft.title = tb.tconst;
            """,
            target_tables=[],
            migration_statements=[],
        )

        self.assertEqual(proposal.status, "no_rewrite_needed")
        self.assertEqual(len(proposal.statements), 2)
        self.assertTrue(proposal.statements[0].startswith("SELECT tb.primarytitle"))
        self.assertTrue(proposal.statements[1].startswith("WITH kft AS"))
        self.assertNotIn("-- Q1", "\n".join(proposal.statements))
        self.assertNotIn("\\set", "\n".join(proposal.statements))

    def test_rewrite_recovers_direct_sql_when_llm_omits_json_wrapper(self) -> None:
        planner = WorkloadPlanner(
            llm=StaticGateway(
                [
                    "SELECT primarytitle FROM title_basics;",
                    "SELECT averagerating FROM title_ratings;",
                ]
            )
        )

        proposal = planner.rewrite(
            "SELECT primarytitle FROM public.title_basics; SELECT averagerating FROM public.title_ratings;",
            target_tables=[{"name": "title_basics"}, {"name": "title_ratings"}],
            migration_statements=[],
        )

        self.assertEqual(proposal.source, "llm_batched_fallback")
        self.assertEqual(len(proposal.statements), 2)

    def test_rewrite_batches_multiple_source_queries(self) -> None:
        gateway = StaticGateway(
            [
                json.dumps(
                    {
                        "status": "planned",
                        "summary": "Q1.",
                        "reasoning": [],
                        "statements": ["SELECT primarytitle FROM title_basics"],
                    }
                ),
                json.dumps(
                    {
                        "status": "planned",
                        "summary": "Q2.",
                        "reasoning": [],
                        "statements": ["SELECT averagerating FROM title_ratings"],
                    }
                ),
            ]
        )
        planner = WorkloadPlanner(llm=gateway)

        proposal = planner.rewrite(
            "SELECT primarytitle FROM public.title_basics; SELECT averagerating FROM public.title_ratings;",
            target_tables=[{"name": "title_basics"}, {"name": "title_ratings"}],
            migration_statements=[],
        )

        self.assertEqual(proposal.source, "llm_batched")
        self.assertEqual(len(proposal.statements), 2)
        self.assertEqual(len(gateway.messages), 2)

    def test_rewrite_reports_batch_timeout_with_context(self) -> None:
        planner = WorkloadPlanner(llm=TimeoutGateway())

        with self.assertRaises(WorkloadRewriteError) as ctx:
            planner.rewrite(
                "SELECT primarytitle FROM public.title_basics;",
                target_tables=[{"name": "title_basics"}],
                migration_statements=[],
            )

        self.assertIn("query batch 1", str(ctx.exception))
        self.assertIn("timed out", str(ctx.exception))

    def test_rewrite_retries_with_repair_prompt_when_llm_returns_prose(self) -> None:
        gateway = StaticGateway(
            [
                "I can rewrite these queries, but here is an explanation instead of JSON.",
                json.dumps(
                    {
                        "status": "planned",
                        "summary": "Repaired.",
                        "reasoning": ["Converted to JSON."],
                        "statements": ["SELECT primarytitle FROM title_basics"],
                    }
                ),
            ]
        )
        planner = WorkloadPlanner(llm=gateway)

        proposal = planner.rewrite(
            "SELECT primarytitle FROM public.title_basics;",
            target_tables=[{"name": "title_basics"}],
            migration_statements=[],
        )

        self.assertEqual(proposal.status, "planned")
        self.assertEqual(proposal.statements, ["SELECT primarytitle FROM title_basics;"])
        self.assertEqual(len(gateway.messages), 2)
        self.assertIn("Initial response:", proposal.raw_response_text)

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
