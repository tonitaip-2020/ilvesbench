from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.osops.workload_files import WorkloadFileParser


class WorkloadFileParserTests(unittest.TestCase):
    def test_workload_file_preserves_constants_in_fingerprints_and_samples(self) -> None:
        content = """
        SELECT title FROM public.title_basics WHERE titletype = 'movie' AND numvotes > 25000 LIMIT 250;
        SELECT title FROM public.title_basics WHERE titletype = 'movie' AND numvotes > 25000 LIMIT 250;
        SELECT title FROM public.title_basics WHERE titletype = 'short' LIMIT 10;
        """.strip()
        parser = WorkloadFileParser()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "workload.sql"
            path.write_text(content, encoding="utf-8")
            summary = parser.parse(path)

        self.assertEqual(summary.source_kind, "workload_file")
        self.assertEqual(summary.statements_detected, 3)
        self.assertEqual(len(summary.top_queries), 2)
        self.assertIn("'movie'", summary.top_queries[0].fingerprint)
        self.assertIn("25000", summary.top_queries[0].fingerprint)
        self.assertNotIn("'?'", summary.top_queries[0].fingerprint)
        self.assertAlmostEqual(summary.top_queries[0].proportion, 2 / 3, places=5)
        self.assertAlmostEqual(summary.top_queries[1].proportion, 1 / 3, places=5)

    def test_workload_file_ignores_pgbench_meta_commands_when_extracting_queries(self) -> None:
        content = """
        \\set id 1
        SELECT * FROM users WHERE id = :id;
        """.strip()
        parser = WorkloadFileParser()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "workload.sql"
            path.write_text(content, encoding="utf-8")
            summary = parser.parse(path)

        self.assertEqual(summary.statements_detected, 1)
        self.assertEqual(summary.top_queries[0].sample_sql, "SELECT * FROM users WHERE id = :id")


if __name__ == "__main__":
    unittest.main()
