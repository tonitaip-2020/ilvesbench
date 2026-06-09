from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.store.workspace import DatabaseWorkspaceStore


class DatabaseWorkspaceStoreTests(unittest.TestCase):
    def test_workspace_persists_database_pair_artifact_and_edited_sql(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DatabaseWorkspaceStore(tmpdir)
            context = store.context(
                source_database="source_db",
                target_database="target_db",
                schemas=["public"],
                workload_source={"selected_path": "workload.sql"},
            )

            store.upsert_artifact(
                context,
                "create_target_schema",
                {"summary": "DDL generated.", "sql_statements": ["CREATE TABLE items (id integer);"]},
                sql_text="CREATE TABLE items (id integer);\n",
                source_run_id="run-123",
            )
            store.save_sql(
                context,
                "create_target_schema",
                "CREATE TABLE items (id bigint);\n",
            )
            status = store.status(context)

            self.assertEqual(status["source_database"], "source_db")
            self.assertEqual(status["target_database"], "target_db")
            self.assertIn("create_target_schema", status["artifacts"])
            artifact = status["artifacts"]["create_target_schema"]
            self.assertEqual(artifact["source"], "user_edited")
            self.assertIn("id bigint", artifact["sql_text"])
            self.assertTrue(Path(status["artifact_records"]["create_target_schema"]["sql_path"]).exists())


if __name__ == "__main__":
    unittest.main()
