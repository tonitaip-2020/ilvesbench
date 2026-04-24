from pathlib import Path
import sys
import tempfile
import unittest


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

        self.assertEqual(summary.statements_detected, 5)
        self.assertEqual(summary.transactions_detected, 1)
        self.assertEqual(summary.multi_statement_transactions, 1)
        self.assertGreaterEqual(len(summary.top_queries), 1)
        self.assertEqual(summary.sampled_transactions[0].statement_count, 2)


if __name__ == "__main__":
    unittest.main()
