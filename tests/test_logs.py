from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.osops.logs import PostgresLogParser


class LogParserTests(unittest.TestCase):
    def test_parser_groups_multi_statement_transaction(self) -> None:
        content = """
2026-04-23 10:00:00.000 UTC [100] LOG:  duration: 0.101 ms  statement: BEGIN
2026-04-23 10:00:00.010 UTC [100] LOG:  duration: 1.234 ms  statement: SELECT * FROM users WHERE id = 1;
2026-04-23 10:00:00.020 UTC [100] LOG:  duration: 2.222 ms  statement: UPDATE users SET name = 'Ada' WHERE id = 1;
2026-04-23 10:00:00.030 UTC [100] LOG:  duration: 0.090 ms  statement: COMMIT
2026-04-23 10:00:01.000 UTC [101] LOG:  duration: 0.500 ms  statement: SELECT * FROM users WHERE id = 2;
        """.strip()
        parser = PostgresLogParser()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "postgresql.log"
            path.write_text(content, encoding="utf-8")
            summary = parser.parse(path)

        self.assertEqual(summary.statements_detected, 3)
        self.assertEqual(summary.transactions_detected, 1)
        self.assertEqual(summary.multi_statement_transactions, 1)
        self.assertGreaterEqual(len(summary.top_queries), 1)
        self.assertEqual(summary.sampled_transactions[0].statement_count, 2)
        self.assertTrue(any("WHERE id = $1" in query.normalized_sql for query in summary.top_queries))

    def test_jsonlog_parser_groups_by_database_and_emits_pgbench_workloads(self) -> None:
        lines = [
            {
                "timestamp": "2026-04-23 10:00:00 UTC",
                "dbname": "db_one",
                "user": "app",
                "pid": 100,
                "application_name": "svc",
                "query": "SELECT * FROM my_table WHERE id = 100;",
                "duration": 1.0,
            },
            {
                "timestamp": "2026-04-23 10:00:01 UTC",
                "dbname": "db_one",
                "user": "app",
                "pid": 100,
                "application_name": "svc",
                "query": "SELECT * FROM my_table WHERE id = 101;",
                "duration": 1.5,
            },
            {
                "timestamp": "2026-04-23 10:00:02 UTC",
                "dbname": "db_two",
                "user": "app",
                "pid": 101,
                "application_name": "svc",
                "query": "SELECT a, b, c FROM table_2 WHERE name LIKE 'asd';",
                "duration": 2.0,
            },
        ]
        parser = PostgresLogParser()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "postgresql.json"
            path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
            summary = parser.parse(path, output_dir=root / "out")

            db_one_manifest = json.loads((root / "out" / "db_one" / "workload.json").read_text(encoding="utf-8"))
            db_two_workload = (root / "out" / "db_two" / "workload.sql").read_text(encoding="utf-8")

        self.assertEqual(summary.statements_detected, 3)
        self.assertEqual(set(summary.workload_outputs), {"db_one", "db_two"})
        self.assertEqual(summary.skipped_summary["total"], 0)
        self.assertEqual(db_one_manifest["database"], "db_one")
        self.assertEqual(db_one_manifest["queries"][0]["ratio"], 1.0)
        self.assertEqual(db_one_manifest["queries"][0]["count"], 2)
        self.assertIn("WHERE id = $1", db_one_manifest["queries"][0]["normalized_sql"])
        self.assertIn("\\set r random(1, 1000000)", db_two_workload)
        self.assertIn("LIKE :q0001_p1", db_two_workload)

    def test_invalid_sql_is_skipped_without_discarding_valid_log_queries(self) -> None:
        lines = [
            {
                "timestamp": "2026-04-23 10:00:00 UTC",
                "dbname": "db_one",
                "query": "SELECT * FROM users WHERE id = 100;",
                "duration": 1.0,
            },
            {
                "timestamp": "2026-04-23 10:00:01 UTC",
                "dbname": "db_one",
                "query": "SELECT FROM ;",
                "duration": 0.5,
            },
        ]
        parser = PostgresLogParser()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "postgresql.json"
            path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
            summary = parser.parse(path)

        self.assertEqual(summary.statements_detected, 2)
        self.assertEqual(len(summary.top_queries), 1)
        self.assertEqual(summary.top_queries[0].count, 1)
        self.assertEqual(summary.skipped_summary["total"], 1)
        self.assertEqual(summary.skipped_summary["by_reason"], {"invalid_sql": 1})
        self.assertEqual(summary.skipped_statements[0]["reason"], "invalid_sql")
        self.assertIn("SELECT FROM", summary.skipped_statements[0]["raw_sql"])
        self.assertIn("Review", summary.skipped_summary["recommended_action"])

    def test_missing_pglast_dependency_is_reported_as_actionable_skip(self) -> None:
        content = "2026-04-23 10:00:00.010 UTC [100] LOG:  duration: 1.234 ms  statement: SELECT * FROM users WHERE id = 1;"
        parser = PostgresLogParser()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "postgresql.log"
            path.write_text(content, encoding="utf-8")
            with patch("ilvesbench.osops.workload.normalizer.parse_sql", None):
                summary = parser.parse(path)

        self.assertEqual(summary.statements_detected, 1)
        self.assertEqual(summary.top_queries, [])
        self.assertEqual(summary.skipped_summary["total"], 1)
        self.assertEqual(summary.skipped_summary["by_reason"], {"normalizer_unavailable": 1})
        self.assertEqual(summary.skipped_statements[0]["reason"], "normalizer_unavailable")
        self.assertIn("Install", summary.skipped_statements[0]["recommended_action"])


if __name__ == "__main__":
    unittest.main()
