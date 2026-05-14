from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import re

from ilvesbench.models import LogSummary, SchemaSnapshot


@dataclass(slots=True)
class IndexRecommendation:
    table: str
    columns: list[str]
    name: str
    sql: str
    reason: str
    confidence: float


@dataclass(slots=True)
class IndexPlan:
    status: str
    summary: str
    recommendations: list[IndexRecommendation] = field(default_factory=list)
    source: str = "heuristic"


class IndexAdvisor:
    """Workload-aware index recommendations for source or normalized target tables."""

    def recommend(
        self,
        *,
        schema: SchemaSnapshot | None,
        target_tables: list[dict],
        workload_sql: str,
        log_summary: LogSummary | None = None,
    ) -> IndexPlan:
        table_columns = self._table_columns(schema, target_tables)
        if not table_columns:
            return IndexPlan(
                status="unavailable",
                summary="Index recommendations need inspected source tables or normalized target tables.",
            )

        statements = self._split_statements(workload_sql)
        if not statements and log_summary is not None:
            statements = [query.sample_sql for query in log_summary.top_queries if query.sample_sql.strip()]
        if not statements:
            return IndexPlan(
                status="no_workload",
                summary="No workload SQL was available for index recommendation.",
            )

        existing_indexed = self._existing_single_column_indexes(schema)
        primary_keys = self._target_primary_keys(target_tables)
        counts: Counter[tuple[str, str]] = Counter()
        reasons: dict[tuple[str, str], set[str]] = {}

        for statement in statements:
            normalized_statement = self._normalize_sql(statement)
            for table_name, columns in table_columns.items():
                if not self._mentions_table(normalized_statement, table_name):
                    continue
                for column in columns:
                    reason = self._column_reason(normalized_statement, column)
                    if not reason:
                        continue
                    key = (table_name, column)
                    counts[key] += 1
                    reasons.setdefault(key, set()).add(reason)

        recommendations: list[IndexRecommendation] = []
        for (table_name, column), count in counts.most_common():
            if (table_name, column) in primary_keys or (table_name, column) in existing_indexed:
                continue
            index_name = self._index_name(table_name, [column])
            reason_text = ", ".join(sorted(reasons.get((table_name, column), [])))
            recommendations.append(
                IndexRecommendation(
                    table=table_name,
                    columns=[column],
                    name=index_name,
                    sql=f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table_name}" ("{column}");',
                    reason=f"{reason_text or 'column appears in workload predicates'} across {count} workload statement(s).",
                    confidence=min(0.95, 0.55 + (count * 0.1)),
                )
            )

        if not recommendations:
            return IndexPlan(
                status="no_recommendations",
                summary="No additional single-column workload indexes were recommended.",
            )

        return IndexPlan(
            status="recommended",
            summary=f"Recommended {len(recommendations)} workload-aware index(es).",
            recommendations=recommendations,
        )

    def _table_columns(self, schema: SchemaSnapshot | None, target_tables: list[dict]) -> dict[str, set[str]]:
        if target_tables:
            result: dict[str, set[str]] = {}
            for table in target_tables:
                table_name = self._identifier(str(table.get("name", "")))
                if not table_name:
                    continue
                columns = {
                    self._identifier(str(column.get("name", "")))
                    for column in table.get("columns", [])
                    if str(column.get("name", "")).strip()
                }
                result[table_name] = {column for column in columns if column}
            return result

        if schema is None:
            return {}

        return {
            self._identifier(table.name): {self._identifier(column.name) for column in table.columns}
            for table in schema.tables
        }

    def _existing_single_column_indexes(self, schema: SchemaSnapshot | None) -> set[tuple[str, str]]:
        if schema is None:
            return set()
        indexed: set[tuple[str, str]] = set()
        for table in schema.tables:
            table_name = self._identifier(table.name)
            for constraint in table.unique_constraints:
                if len(constraint.columns) == 1:
                    indexed.add((table_name, self._identifier(constraint.columns[0])))
            for index in table.indexes:
                match = re.search(r"\((?P<columns>[^)]+)\)", index.definition)
                if not match:
                    continue
                columns = [self._identifier(item.strip().strip('"')) for item in match.group("columns").split(",")]
                if len(columns) == 1 and columns[0]:
                    indexed.add((table_name, columns[0]))
        return indexed

    def _target_primary_keys(self, target_tables: list[dict]) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for table in target_tables:
            table_name = self._identifier(str(table.get("name", "")))
            primary_key = [self._identifier(str(item)) for item in table.get("primary_key", [])]
            if len(primary_key) == 1 and table_name and primary_key[0]:
                keys.add((table_name, primary_key[0]))
        return keys

    def _mentions_table(self, statement: str, table_name: str) -> bool:
        table_pattern = re.escape(table_name)
        return bool(re.search(rf'(?<![a-z0-9_])"?{table_pattern}"?(?![a-z0-9_])', statement, re.IGNORECASE))

    def _column_reason(self, statement: str, column: str) -> str:
        column_pattern = re.escape(column)
        quoted_or_plain = rf'(?:"{column_pattern}"|{column_pattern})'
        if re.search(rf"\bwhere\b.*{quoted_or_plain}\s*(=|<|>|<=|>=|<>|!=|\bin\b|\blike\b|\bilike\b|\bbetween\b)", statement, re.IGNORECASE | re.DOTALL):
            return "filter predicate"
        if re.search(rf"\bjoin\b.*\bon\b.*{quoted_or_plain}\s*=", statement, re.IGNORECASE | re.DOTALL):
            return "join predicate"
        if re.search(rf"\border\s+by\b[^;]*{quoted_or_plain}", statement, re.IGNORECASE):
            return "ORDER BY"
        if re.search(rf"\bgroup\s+by\b[^;]*{quoted_or_plain}", statement, re.IGNORECASE):
            return "GROUP BY"
        return ""

    def _split_statements(self, workload_sql: str) -> list[str]:
        return [statement.strip() for statement in workload_sql.split(";") if statement.strip()]

    def _normalize_sql(self, sql: str) -> str:
        return re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()

    def _identifier(self, value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()

    def _index_name(self, table_name: str, columns: list[str]) -> str:
        raw = "idx_" + table_name + "_" + "_".join(columns)
        return self._identifier(raw)[:63]
