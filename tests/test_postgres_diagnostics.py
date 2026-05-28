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

    def test_abstract_profile_does_not_return_table_or_column_names(self) -> None:
        inspector = PostgresInspector(PostgresConfig(schemas=["public"]))
        profile = inspector._build_abstract_profile(
            "source_db",
            {
                "tables": [
                    {
                        "table_schema": "public",
                        "table_name": "sensitive_orders",
                        "column_count": 3,
                        "nullable_column_count": 1,
                        "defaulted_column_count": 0,
                    },
                    {
                        "table_schema": "public",
                        "table_name": "sensitive_customers",
                        "column_count": 2,
                        "nullable_column_count": 0,
                        "defaulted_column_count": 1,
                    },
                ],
                "data_types": [
                    {"table_schema": "public", "table_name": "sensitive_orders", "data_type": "integer", "count": 1},
                    {"table_schema": "public", "table_name": "sensitive_orders", "data_type": "text", "count": 2},
                ],
                "constraints": [
                    {
                        "table_schema": "public",
                        "table_name": "sensitive_orders",
                        "constraint_name": "orders_pkey",
                        "constraint_type": "PRIMARY KEY",
                        "referenced_table_schema": None,
                        "referenced_table_name": None,
                    },
                    {
                        "table_schema": "public",
                        "table_name": "sensitive_orders",
                        "constraint_name": "orders_customer_fkey",
                        "constraint_type": "FOREIGN KEY",
                        "referenced_table_schema": "public",
                        "referenced_table_name": "sensitive_customers",
                    },
                ],
                "index_count": 3,
            },
            [{"total_bytes": 8192}],
        )

        self.assertTrue(profile["privacy"]["abstracted"])
        self.assertEqual(profile["counts"]["tables"], 2)
        self.assertEqual(profile["counts"]["foreign_keys"], 1)
        self.assertEqual(profile["counts"]["indexes"], 3)
        rendered = str(profile)
        self.assertNotIn("sensitive_orders", rendered)
        self.assertNotIn("sensitive_customers", rendered)


if __name__ == "__main__":
    unittest.main()
