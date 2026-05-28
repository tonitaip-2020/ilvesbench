from __future__ import annotations

from dataclasses import dataclass, field
import re
from statistics import mean

from ilvesbench.models import SchemaSnapshot, TableMetadata


TEXT_TYPES = {
    "text",
    "character varying",
    "varchar",
    "character",
    "char",
    "citext",
}
COLLECTION_TYPE_RE = re.compile(r"(\[\]|\bARRAY\b|\bjsonb?\b)", re.IGNORECASE)
TOKEN_PATTERNS = {
    "identifier": re.compile(r"^[A-Za-z]{1,8}[0-9][A-Za-z0-9_-]*$"),
    "numeric": re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$"),
    "uuid": re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    "short_code": re.compile(r"^[A-Za-z0-9_-]{1,24}$"),
}
DELIMITERS = [",", "|", ";", "\n", "\t"]
LIST_NAME_RE = re.compile(r"(?:^|_)(ids?|codes?|names?|tags?|list|csv|values?|items?|members?|directors?|writers?|genres?)(?:$|_)", re.IGNORECASE)


@dataclass(slots=True)
class FirstNormalFormFinding:
    table: str
    column: str
    severity: str
    confidence: float
    pattern: str
    summary: str
    recommendation: str
    evidence: dict = field(default_factory=dict)
    candidate_reference: dict | None = None


@dataclass(slots=True)
class FirstNormalFormScan:
    status: str
    summary: str
    findings: list[FirstNormalFormFinding] = field(default_factory=list)
    source: str = "deterministic"


class FirstNormalFormScanner:
    """Heuristic detector for common 1NF data-shape warnings."""

    def schema_findings(self, schema: SchemaSnapshot) -> list[FirstNormalFormFinding]:
        findings: list[FirstNormalFormFinding] = []
        for table in schema.tables:
            table_name = f"{table.schema}.{table.name}"
            for column in table.columns:
                data_type = column.data_type.lower()
                if not COLLECTION_TYPE_RE.search(data_type):
                    continue
                findings.append(
                    FirstNormalFormFinding(
                        table=table_name,
                        column=column.name,
                        severity="warning",
                        confidence=0.86,
                        pattern="collection_typed_column",
                        summary=(
                            f"{table_name}.{column.name} uses {column.data_type}, which can store multiple logical values "
                            "inside one column."
                        ),
                        recommendation=(
                            "Review whether this column represents a repeated attribute. If so, split it into a child "
                            "table with one row per value."
                        ),
                        evidence={"data_type": column.data_type},
                    )
                )
        return findings

    def analyze_column_values(
        self,
        table_name: str,
        column_name: str,
        data_type: str,
        values: list[str],
    ) -> FirstNormalFormFinding | None:
        if not self._is_text_type(data_type):
            return None

        non_empty = [str(value).strip() for value in values if str(value).strip()]
        if len(non_empty) < 3:
            return None

        delimiter_stats = [
            self._delimiter_stats(delimiter, non_empty)
            for delimiter in DELIMITERS
        ]
        candidates = [item for item in delimiter_stats if item["compound_rows"] > 0]
        if not candidates:
            return None

        best = max(
            candidates,
            key=lambda item: (
                item["compound_ratio"],
                item["avg_tokens_when_compound"],
                item["compound_rows"],
            ),
        )
        tokens = best["tokens"]
        token_pattern, token_pattern_ratio = self._token_pattern(tokens)
        column_name_suggests_list = bool(LIST_NAME_RE.search(column_name))
        likely_false_positive = (
            best["delimiter"] == ","
            and token_pattern == "free_text"
            and best["avg_token_length"] >= 18
            and not column_name_suggests_list
        )

        strong_signal = (
            best["compound_rows"] >= 3
            and best["compound_ratio"] >= 0.05
            and token_pattern in {"identifier", "numeric", "uuid", "short_code"}
            and token_pattern_ratio >= 0.65
        )
        name_signal = (
            column_name_suggests_list
            and best["compound_rows"] >= 1
            and token_pattern in {"identifier", "numeric", "uuid", "short_code"}
            and token_pattern_ratio >= 0.55
        )
        broad_signal = best["compound_rows"] >= 10 and best["compound_ratio"] >= 0.2 and not likely_false_positive
        if not (strong_signal or name_signal or broad_signal):
            return None

        confidence = 0.55
        if strong_signal:
            confidence += 0.2
        if name_signal:
            confidence += 0.12
        if best["compound_ratio"] >= 0.25:
            confidence += 0.08
        if likely_false_positive:
            confidence -= 0.2
        confidence = round(max(0.35, min(0.95, confidence)), 2)

        delimiter_label = self._delimiter_label(best["delimiter"])
        severity = "warning" if confidence >= 0.65 else "info"
        summary = (
            f"{table_name}.{column_name} appears to store multiple {delimiter_label}-separated values "
            f"in {round(best['compound_ratio'] * 100, 1)}% of sampled rows."
        )
        recommendation = (
            "Consider replacing this column with a child relation containing the parent key and one row per extracted value."
        )
        return FirstNormalFormFinding(
            table=table_name,
            column=column_name,
            severity=severity,
            confidence=confidence,
            pattern="delimited_multi_value_column",
            summary=summary,
            recommendation=recommendation,
            evidence={
                "data_type": data_type,
                "sampled_non_empty_rows": len(non_empty),
                "delimiter": best["delimiter"],
                "delimiter_label": delimiter_label,
                "compound_rows": best["compound_rows"],
                "compound_ratio": round(best["compound_ratio"], 4),
                "avg_tokens_when_compound": round(best["avg_tokens_when_compound"], 3),
                "max_tokens": best["max_tokens"],
                "token_pattern": token_pattern,
                "token_pattern_ratio": round(token_pattern_ratio, 4),
                "token_samples": tokens[:12],
                "column_name_suggests_list": column_name_suggests_list,
            },
        )

    def build_scan(self, findings: list[FirstNormalFormFinding]) -> FirstNormalFormScan:
        if findings:
            warning_count = sum(1 for item in findings if item.severity == "warning")
            return FirstNormalFormScan(
                status="warnings_found",
                summary=f"Found {len(findings)} possible 1NF warning(s), including {warning_count} warning-level finding(s).",
                findings=sorted(findings, key=lambda item: item.confidence, reverse=True),
            )
        return FirstNormalFormScan(
            status="no_warnings",
            summary="No common 1NF data-shape warnings were detected in sampled columns.",
        )

    def candidate_key_columns(self, schema: SchemaSnapshot) -> list[tuple[TableMetadata, str]]:
        candidates: list[tuple[TableMetadata, str]] = []
        for table in schema.tables:
            valid_columns = {
                column.name
                for column in table.columns
                if self._is_text_type(column.data_type)
            }
            for constraint in table.unique_constraints:
                if len(constraint.columns) == 1 and constraint.columns[0] in valid_columns:
                    candidates.append((table, constraint.columns[0]))
        return candidates

    def should_sample_column(self, data_type: str) -> bool:
        return self._is_text_type(data_type)

    def _delimiter_stats(self, delimiter: str, values: list[str]) -> dict:
        compound_rows = 0
        token_counts: list[int] = []
        tokens: list[str] = []
        token_lengths: list[int] = []
        for value in values:
            parts = [part.strip() for part in value.split(delimiter)]
            row_tokens = [part for part in parts if part]
            if len(row_tokens) < 2:
                continue
            compound_rows += 1
            token_counts.append(len(row_tokens))
            tokens.extend(row_tokens)
            token_lengths.extend(len(token) for token in row_tokens)
        return {
            "delimiter": delimiter,
            "compound_rows": compound_rows,
            "compound_ratio": compound_rows / len(values) if values else 0.0,
            "avg_tokens_when_compound": mean(token_counts) if token_counts else 0,
            "max_tokens": max(token_counts) if token_counts else 0,
            "avg_token_length": mean(token_lengths) if token_lengths else 0,
            "tokens": list(dict.fromkeys(tokens))[:80],
        }

    def _token_pattern(self, tokens: list[str]) -> tuple[str, float]:
        if not tokens:
            return "unknown", 0.0
        scores = {
            name: sum(1 for token in tokens if pattern.match(token)) / len(tokens)
            for name, pattern in TOKEN_PATTERNS.items()
        }
        pattern_name, ratio = max(scores.items(), key=lambda item: item[1])
        if ratio < 0.55:
            return "free_text", ratio
        return pattern_name, ratio

    def _is_text_type(self, data_type: str) -> bool:
        normalized = data_type.lower().strip()
        return normalized in TEXT_TYPES or "character varying" in normalized or normalized.startswith("varchar")

    def _delimiter_label(self, delimiter: str) -> str:
        return {
            ",": "comma",
            "|": "pipe",
            ";": "semicolon",
            "\n": "newline",
            "\t": "tab",
        }.get(delimiter, delimiter)
