from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.benchmark.normal_form import FirstNormalFormScanner
from ilvesbench.models import ColumnMetadata, SchemaSnapshot, TableMetadata


class FirstNormalFormScannerTests(unittest.TestCase):
    def test_detects_delimited_identifier_lists(self) -> None:
        scanner = FirstNormalFormScanner()

        finding = scanner.analyze_column_values(
            "public.title_crew",
            "directors",
            "text",
            [
                "nm0000001,nm0000002",
                "nm0000003",
                "nm0000004,nm0000005,nm0000006",
                "nm0000007,nm0000008",
            ],
        )

        self.assertIsNotNone(finding)
        self.assertEqual(finding.pattern, "delimited_multi_value_column")
        self.assertEqual(finding.evidence["delimiter"], ",")
        self.assertEqual(finding.evidence["token_pattern"], "identifier")
        self.assertGreaterEqual(finding.confidence, 0.65)

    def test_avoids_short_free_text_comma_false_positive(self) -> None:
        scanner = FirstNormalFormScanner()

        finding = scanner.analyze_column_values(
            "public.movies",
            "title",
            "text",
            [
                "Hello, Dolly!",
                "Paris, Texas",
                "The Cook, the Thief, His Wife & Her Lover",
                "No comma title",
            ],
        )

        self.assertIsNone(finding)

    def test_flags_schema_collection_types(self) -> None:
        scanner = FirstNormalFormScanner()
        schema = SchemaSnapshot(
            database="demo",
            collected_at="2026-05-28T00:00:00+00:00",
            tables=[
                TableMetadata(
                    schema="public",
                    name="events",
                    columns=[
                        ColumnMetadata(name="id", data_type="integer", is_nullable=False),
                        ColumnMetadata(name="payload", data_type="jsonb", is_nullable=True),
                    ],
                )
            ],
        )

        findings = scanner.schema_findings(schema)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].pattern, "collection_typed_column")


if __name__ == "__main__":
    unittest.main()
