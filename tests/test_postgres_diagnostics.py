from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.config import PostgresConfig
from ilvesbench.db.postgres import PostgresInspector


class PostgresDiagnosticsTests(unittest.TestCase):
    def test_connection_hints_cover_common_setup_errors(self) -> None:
        inspector = PostgresInspector(PostgresConfig())

        self.assertIn("user/password", inspector._connection_hint("password authentication failed for user llm"))
        self.assertIn("pg_hba.conf", inspector._connection_hint("no pg_hba.conf entry for host"))
        self.assertIn("container is running", inspector._connection_hint("connection refused"))
        self.assertIn("original_database/admin_database", inspector._connection_hint('database "mvp_db" does not exist'))
        self.assertIn("role privileges", inspector._connection_hint("permission denied for schema public"))


if __name__ == "__main__":
    unittest.main()
