from pathlib import Path
import json
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.benchmark.schema_transformer import SchemaTransformer
from ilvesbench.models import ColumnMetadata, LLMResult, SchemaSnapshot, TableMetadata, UniqueConstraintMetadata


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


class SchemaTransformerTests(unittest.TestCase):
    def test_detects_repeating_groups_as_normalization_candidate(self) -> None:
        schema = SchemaSnapshot(
            database="demo",
            collected_at="2026-04-23T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="customers",
                    columns=[
                        ColumnMetadata(name="customer_id", data_type="integer", is_nullable=False),
                        ColumnMetadata(name="phone1", data_type="text", is_nullable=True),
                        ColumnMetadata(name="phone2", data_type="text", is_nullable=True),
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="customers_pkey", columns=["customer_id"]),
                    ],
                )
            ],
        )

        proposal = SchemaTransformer().analyze(schema)

        self.assertEqual(proposal.status, "candidate_normalization")
        self.assertTrue(any("repeated numbered columns" in finding for finding in proposal.table_findings))

    def test_reports_appears_3nf_for_keyed_schema_without_obvious_warnings(self) -> None:
        schema = SchemaSnapshot(
            database="demo",
            collected_at="2026-04-23T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="customers",
                    columns=[
                        ColumnMetadata(name="customer_id", data_type="integer", is_nullable=False),
                        ColumnMetadata(name="email", data_type="text", is_nullable=False),
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="customers_pkey", columns=["customer_id"]),
                        UniqueConstraintMetadata(name="customers_email_key", columns=["email"]),
                    ],
                )
            ],
        )

        proposal = SchemaTransformer().analyze(schema)

        self.assertEqual(proposal.status, "appears_3nf")

    def test_1nf_findings_make_metadata_fallback_candidate(self) -> None:
        schema = SchemaSnapshot(
            database="demo",
            collected_at="2026-04-23T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="title_crew",
                    columns=[
                        ColumnMetadata(name="tconst", data_type="text", is_nullable=False),
                        ColumnMetadata(name="directors", data_type="text", is_nullable=True),
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="title_crew_pkey", columns=["tconst"]),
                    ],
                )
            ],
        )

        proposal = SchemaTransformer().analyze(
            schema,
            [
                {
                    "table": "public.title_crew",
                    "column": "directors",
                    "confidence": 0.82,
                    "pattern": "delimited_multi_value_column",
                    "summary": "public.title_crew.directors appears to store multiple comma-separated values.",
                    "evidence": {"delimiter": ","},
                }
            ],
        )

        self.assertEqual(proposal.status, "candidate_normalization")
        self.assertTrue(any("comma-separated" in finding for finding in proposal.table_findings))
        self.assertTrue(any('CREATE TABLE IF NOT EXISTS "title_crew_directors"' in statement for statement in proposal.sql_statements))
        self.assertTrue(any(table["name"] == "title_crew_directors" for table in proposal.target_tables))

    def test_llm_noop_with_1nf_findings_gets_deterministic_ddl(self) -> None:
        schema = SchemaSnapshot(
            database="demo",
            collected_at="2026-04-23T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="name_basics",
                    columns=[
                        ColumnMetadata(name="nconst", data_type="text", is_nullable=False),
                        ColumnMetadata(name="primaryname", data_type="text", is_nullable=True),
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="name_basics_pkey", columns=["nconst"]),
                    ],
                ),
                TableMetadata(
                    schema="public",
                    name="title_crew",
                    columns=[
                        ColumnMetadata(name="tconst", data_type="text", is_nullable=False),
                        ColumnMetadata(name="directors", data_type="text", is_nullable=True),
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="title_crew_pkey", columns=["tconst"]),
                    ],
                ),
            ],
        )
        payload = {
            "assessment": {
                "status": "appears_3nf",
                "summary": "No decomposition recommended.",
                "reasoning": [],
            },
            "table_findings": [],
            "functional_dependencies": [],
            "target_tables": [],
        }

        proposal = SchemaTransformer(llm=StaticGateway(payload)).analyze(
            schema,
            [
                {
                    "table": "public.title_crew",
                    "column": "directors",
                    "confidence": 0.95,
                    "pattern": "delimited_multi_value_column",
                    "summary": "public.title_crew.directors appears to store multiple comma-separated values.",
                    "evidence": {"delimiter": ","},
                    "candidate_reference": {"table": "public.name_basics", "column": "nconst"},
                }
            ],
        )

        self.assertEqual(proposal.status, "candidate_normalization")
        self.assertEqual(proposal.source, "deterministic_1nf_fallback_after_llm")
        self.assertTrue(any(table["name"] == "title_crew_directors" for table in proposal.target_tables))
        self.assertTrue(any("REFERENCES \"name_basics\" (\"nconst\")" in statement for statement in proposal.sql_statements))

    def test_1nf_fallback_infers_parent_key_without_declared_constraints(self) -> None:
        schema = SchemaSnapshot(
            database="demo",
            collected_at="2026-04-23T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="title_crew",
                    columns=[
                        ColumnMetadata(name="tconst", data_type="text", is_nullable=False),
                        ColumnMetadata(name="directors", data_type="text", is_nullable=True),
                        ColumnMetadata(name="writers", data_type="text", is_nullable=True),
                    ],
                    unique_constraints=[],
                ),
            ],
        )

        proposal = SchemaTransformer().analyze(
            schema,
            [
                {
                    "table": "public.title_crew",
                    "column": "directors",
                    "confidence": 0.95,
                    "pattern": "delimited_multi_value_column",
                    "summary": "public.title_crew.directors appears to store multiple comma-separated values.",
                    "evidence": {"delimiter": ","},
                },
                {
                    "table": "public.title_crew",
                    "column": "writers",
                    "confidence": 0.95,
                    "pattern": "delimited_multi_value_column",
                    "summary": "public.title_crew.writers appears to store multiple comma-separated values.",
                    "evidence": {"delimiter": ","},
                },
            ],
        )

        self.assertEqual(proposal.status, "candidate_normalization")
        self.assertTrue(any(table["name"] == "title_crew_directors" for table in proposal.target_tables))
        self.assertTrue(any(table["name"] == "title_crew_writers" for table in proposal.target_tables))
        self.assertTrue(any('"tconst"' in statement and '"director"' in statement for statement in proposal.sql_statements))
        self.assertFalse(any("FOREIGN KEY" in statement for statement in proposal.sql_statements))

    def test_llm_proposal_generates_target_table_sql(self) -> None:
        schema = SchemaSnapshot(
            database="demo",
            collected_at="2026-04-23T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="car",
                    columns=[
                        ColumnMetadata(name="id", data_type="integer", is_nullable=False),
                        ColumnMetadata(name="make", data_type="text", is_nullable=False),
                        ColumnMetadata(name="model", data_type="text", is_nullable=False),
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="car_pkey", columns=["id"]),
                    ],
                )
            ],
        )
        payload = {
            "assessment": {
                "status": "candidate_normalization",
                "summary": "Normalized from 1 table to 2 tables.",
                "reasoning": ["model determines make in the intended domain."],
            },
            "table_findings": ["public.car mixes model facts with car rows."],
            "functional_dependencies": [
                {
                    "table": "public.car",
                    "determinant": ["model"],
                    "dependent": ["make"],
                    "confidence": "medium",
                    "reason": "Car models usually imply make in this simplified domain.",
                }
            ],
            "target_tables": [
                {
                    "name": "car_model",
                    "purpose": "Stores model-level attributes.",
                    "source_tables": ["public.car"],
                    "columns": [
                        {"source_table": "public.car", "source_column": "model", "name": "model"},
                        {"source_table": "public.car", "source_column": "make", "name": "make"},
                    ],
                    "primary_key": ["model"],
                    "uniques": [],
                    "foreign_keys": [],
                },
                {
                    "name": "car",
                    "purpose": "Stores each car row.",
                    "source_tables": ["public.car"],
                    "columns": [
                        {"source_table": "public.car", "source_column": "id", "name": "id"},
                        {"source_table": "public.car", "source_column": "model", "name": "model"},
                    ],
                    "primary_key": ["id"],
                    "uniques": [],
                    "foreign_keys": [
                        {
                            "columns": ["model"],
                            "references_table": "car_model",
                            "references_columns": ["model"],
                        }
                    ],
                },
            ],
            "decomposition_summary": "Normalized from 1 table to 2 tables.",
        }

        proposal = SchemaTransformer(
            llm=StaticGateway(payload),
            target_database="demo_new",
        ).analyze(schema)

        self.assertEqual(proposal.status, "candidate_normalization")
        self.assertEqual(proposal.source, "llm")
        self.assertEqual(len(proposal.sql_statements), 2)
        self.assertTrue(any('CREATE TABLE IF NOT EXISTS "car_model"' in statement for statement in proposal.sql_statements))
        self.assertTrue(any('FOREIGN KEY ("model") REFERENCES "car_model" ("model")' in statement for statement in proposal.sql_statements))

    def test_llm_proposal_rejects_type_drifting_column_remap(self) -> None:
        schema = SchemaSnapshot(
            database="demo",
            collected_at="2026-04-23T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="studentcourses",
                    columns=[
                        ColumnMetadata(name="student_id", data_type="integer", is_nullable=False),
                        ColumnMetadata(name="course_code", data_type="integer", is_nullable=False),
                        ColumnMetadata(name="course_name", data_type="character varying", is_nullable=False),
                    ],
                    unique_constraints=[
                        UniqueConstraintMetadata(name="studentcourses_student_course_key", columns=["student_id", "course_code"]),
                    ],
                )
            ],
        )
        payload = {
            "assessment": {
                "status": "candidate_normalization",
                "summary": "Bad decomposition.",
                "reasoning": ["Synthetic invalid proposal."],
            },
            "table_findings": [],
            "functional_dependencies": [],
            "target_tables": [
                {
                    "name": "courses",
                    "purpose": "Stores course lookup rows.",
                    "source_tables": ["public.studentcourses"],
                    "columns": [
                        {"source_table": "public.studentcourses", "source_column": "course_name", "name": "course_code"},
                        {"source_table": "public.studentcourses", "source_column": "course_name", "name": "course_name"},
                    ],
                    "primary_key": ["course_code"],
                    "uniques": [],
                    "foreign_keys": [],
                }
            ],
            "decomposition_summary": "Bad decomposition.",
        }

        proposal = SchemaTransformer(
            llm=StaticGateway(payload),
            target_database="demo_new",
        ).analyze(schema)

        self.assertEqual(proposal.source, "metadata_fallback")
        self.assertIn("fell back to metadata-only analysis", proposal.summary)


if __name__ == "__main__":
    unittest.main()
