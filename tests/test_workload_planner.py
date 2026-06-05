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

        self.assertIn("executable rewritten SQL", str(ctx.exception))

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
                    "SELECT primarytitle FROM title_basics; SELECT averagerating FROM title_ratings;",
                ]
            )
        )

        proposal = planner.rewrite(
            "SELECT primarytitle FROM public.title_basics; SELECT averagerating FROM public.title_ratings;",
            target_tables=[{"name": "title_basics"}, {"name": "title_ratings"}],
            migration_statements=[],
        )

        self.assertEqual(proposal.source, "llm_sql_fallback")
        self.assertEqual(len(proposal.statements), 2)

    def test_rewrite_sends_multiple_source_queries_in_one_call(self) -> None:
        gateway = StaticGateway(
            {
                "status": "planned",
                "summary": "Both queries.",
                "reasoning": [],
                "statements": [
                    "SELECT primarytitle FROM title_basics",
                    "SELECT averagerating FROM title_ratings",
                ],
            }
        )
        planner = WorkloadPlanner(llm=gateway)

        proposal = planner.rewrite(
            "SELECT primarytitle FROM public.title_basics; SELECT averagerating FROM public.title_ratings;",
            target_tables=[{"name": "title_basics"}, {"name": "title_ratings"}],
            migration_statements=[],
        )

        self.assertEqual(proposal.source, "llm")
        self.assertEqual(len(proposal.statements), 2)
        self.assertEqual(len(gateway.messages), 1)

    def test_rewrite_prompt_includes_source_and_target_schema(self) -> None:
        gateway = StaticGateway(
            {
                "status": "planned",
                "summary": "Rewritten.",
                "reasoning": [],
                "statements": ["SELECT i.id FROM items_new i"],
            }
        )
        planner = WorkloadPlanner(llm=gateway)

        planner.rewrite(
            "SELECT id FROM public.items;",
            source_tables=[
                {
                    "schema": "public",
                    "name": "items",
                    "columns": [{"name": "id", "data_type": "integer"}],
                }
            ],
            target_tables=[
                {
                    "name": "items_new",
                    "columns": [{"name": "id", "data_type": "integer"}],
                }
            ],
            migration_statements=[],
        )

        prompt = gateway.messages[0][1]["content"]
        self.assertIn("Source schema:", prompt)
        self.assertIn("CREATE TABLE public.items", prompt)
        self.assertIn('"id" integer', prompt)
        self.assertIn("Target schema:", prompt)
        self.assertIn("CREATE TABLE items_new", prompt)

    def test_repair_rewrite_prompt_includes_validation_error(self) -> None:
        gateway = StaticGateway(["SELECT id FROM items_new;"])
        planner = WorkloadPlanner(llm=gateway)

        proposal = planner.repair_rewrite(
            "SELECT id FROM public.items;",
            "SELECT missing FROM items_new;",
            {"status": "failed", "error": "column missing does not exist"},
            target_tables=[{"name": "items_new", "columns": [{"name": "id"}]}],
            migration_statements=[],
            query_index=1,
            source_tables=[{"schema": "public", "name": "items", "columns": [{"name": "id"}]}],
        )

        prompt = gateway.messages[0][1]["content"]
        self.assertEqual(proposal.statements, ["SELECT id FROM items_new;"])
        self.assertIn("PostgreSQL validation error", prompt)
        self.assertIn("column missing does not exist", prompt)
        self.assertIn("Previous rejected rewrite", prompt)
        self.assertIn("SELECT missing FROM items_new", prompt)

    def test_rewrite_splits_large_workloads_into_limited_batches(self) -> None:
        responses = []
        for batch_start in (1, 11, 21):
            responses.append(
                " ".join(
                    f"SELECT {value} FROM rewritten_table;"
                    for value in range(batch_start, batch_start + 10)
                )
            )
        gateway = StaticGateway(responses)
        planner = WorkloadPlanner(llm=gateway)
        workload_sql = " ".join(
            f"SELECT {value} FROM source_table;"
            for value in range(1, 31)
        )

        proposal = planner.rewrite(
            workload_sql,
            target_tables=[{"name": "rewritten_table"}],
            migration_statements=[],
            batch_size=10,
        )

        self.assertEqual(len(proposal.statements), 30)
        self.assertEqual(len(gateway.messages), 3)
        self.assertEqual(proposal.request_payload["batch_count"], 3)

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
        self.assertEqual(ctx.exception.request_payload["source_statements"], ["SELECT primarytitle FROM public.title_basics;"])

    def test_rewrite_rejects_prose_without_executable_sql(self) -> None:
        gateway = StaticGateway(
            [
                "I can rewrite these queries, but here is an explanation instead of JSON.",
            ]
        )
        planner = WorkloadPlanner(llm=gateway)

        with self.assertRaises(WorkloadRewriteError):
            planner.rewrite(
                "SELECT primarytitle FROM public.title_basics;",
                target_tables=[{"name": "title_basics"}],
                migration_statements=[],
            )
        self.assertEqual(len(gateway.messages), 1)

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
