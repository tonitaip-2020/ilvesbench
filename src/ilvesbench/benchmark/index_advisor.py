from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import re

from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import LogSummary, SchemaSnapshot


@dataclass(slots=True)
class IndexRecommendation:
    action: str
    table: str
    columns: list[str]
    name: str
    sql: str
    reason: str
    confidence: float
    index_type: str = "btree"


@dataclass(slots=True)
class IndexPlan:
    status: str
    summary: str
    recommendations: list[IndexRecommendation] = field(default_factory=list)
    source: str = "heuristic"
    raw_response_text: str = ""
    request_payload: dict | None = None


class IndexAdvisor:
    """Workload-aware index recommendations for source or normalized target tables."""

    def __init__(
        self,
        llm: LLMGateway | None = None,
        *,
        query_batch_size: int = 25,
        index_batch_size: int = 80,
    ) -> None:
        self._llm = llm
        self.query_batch_size = query_batch_size
        self.index_batch_size = index_batch_size

    def recommend(
        self,
        *,
        schema: SchemaSnapshot | None,
        target_tables: list[dict],
        workload_sql: str,
        log_summary: LogSummary | None = None,
        use_llm: bool = False,
    ) -> IndexPlan:
        if use_llm and self._llm is not None:
            try:
                return self._recommend_with_llm(
                    schema=schema,
                    target_tables=target_tables,
                    workload_sql=workload_sql,
                    log_summary=log_summary,
                )
            except Exception as exc:
                fallback = self._recommend_with_heuristics(
                    schema=schema,
                    target_tables=target_tables,
                    workload_sql=workload_sql,
                    log_summary=log_summary,
                )
                fallback.source = "heuristic_fallback_after_llm_error"
                fallback.summary = f"LLM index recommendation failed, so heuristic recommendations were used: {exc}"
                return fallback
        return self._recommend_with_heuristics(
            schema=schema,
            target_tables=target_tables,
            workload_sql=workload_sql,
            log_summary=log_summary,
        )

    def _recommend_with_heuristics(
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
                    action="create",
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

    def _recommend_with_llm(
        self,
        *,
        schema: SchemaSnapshot | None,
        target_tables: list[dict],
        workload_sql: str,
        log_summary: LogSummary | None = None,
    ) -> IndexPlan:
        statements = self._split_statements(workload_sql)
        if not statements and log_summary is not None:
            statements = [query.sample_sql for query in log_summary.top_queries if query.sample_sql.strip()]
        if not statements:
            return IndexPlan(status="no_workload", summary="No workload SQL was available for index recommendation.", source="llm")

        structure = self._structure_payload(schema, target_tables)
        if not structure:
            return IndexPlan(status="unavailable", summary="Index recommendations need target database structure.", source="llm")

        current_indices = self._current_index_payload(schema)
        query_chunks = self._chunks(statements, self.query_batch_size)
        index_chunks = self._chunks(current_indices, self.index_batch_size) or [[]]
        recommendations: list[IndexRecommendation] = []
        request_batches: list[dict] = []
        raw_responses: list[str] = []
        for query_index, query_chunk in enumerate(query_chunks, start=1):
            for index_index, index_chunk in enumerate(index_chunks, start=1):
                request_payload = {
                    "mode": "index_recommendations",
                    "query_batch": query_index,
                    "query_batch_count": len(query_chunks),
                    "index_batch": index_index,
                    "index_batch_count": len(index_chunks),
                    "queries": query_chunk,
                    "database_structure": structure,
                    "current_indices": index_chunk,
                }
                llm_result = self._llm.generate(
                    self._build_messages(request_payload),
                    max_tokens=4096,
                )
                request_batches.append(request_payload)
                raw_responses.append(f"Query batch {query_index}, index batch {index_index}:\n{llm_result.response_text}")
                recommendations.extend(self._recommendations_from_response(llm_result.response_text, structure, current_indices))

        recommendations = self._dedupe_recommendations(recommendations)
        if not recommendations:
            return IndexPlan(
                status="no_recommendations",
                summary="The LLM did not recommend index changes for the current workload.",
                source="llm",
                raw_response_text="\n\n".join(raw_responses),
                request_payload={"batches": request_batches},
            )
        return IndexPlan(
            status="recommended",
            summary=f"Recommended {len(recommendations)} index change(s): "
            f"{sum(1 for item in recommendations if item.action == 'create')} create, "
            f"{sum(1 for item in recommendations if item.action == 'drop')} drop.",
            recommendations=recommendations,
            source="llm",
            raw_response_text="\n\n".join(raw_responses),
            request_payload={"batches": request_batches},
        )

    def _build_messages(self, payload: dict) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "/no_think\n"
                    "You recommend PostgreSQL index changes for a workload. Return JSON only. "
                    "Do not include markdown or explanations outside JSON."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Recommend index changes for the target PostgreSQL database.\n\n"
                    "Rules:\n"
                    "1. Recommend CREATE INDEX statements only when supported by the workload predicates, joins, ordering, or grouping.\n"
                    "2. Recommend DROP INDEX statements only for clearly redundant or unused-looking non-primary indexes. "
                    "Do not drop primary-key or unique-constraint indexes.\n"
                    "3. Do not use CREATE INDEX CONCURRENTLY or DROP INDEX CONCURRENTLY.\n"
                    "4. SQL must contain only CREATE INDEX, CREATE INDEX IF NOT EXISTS, DROP INDEX, or DROP INDEX IF EXISTS.\n"
                    "5. Prefer B-tree indexes unless the workload clearly justifies another PostgreSQL index type.\n"
                    "6. If unsure, omit the recommendation.\n\n"
                    "Return JSON with this shape:\n"
                    "{\n"
                    '  "status": "recommended|no_recommendations",\n'
                    '  "summary": "short summary",\n'
                    '  "recommendations": [\n'
                    "    {\n"
                    '      "action": "create|drop",\n'
                    '      "table": "table_name",\n'
                    '      "columns": ["col"],\n'
                    '      "name": "index_name",\n'
                    '      "sql": "CREATE INDEX IF NOT EXISTS ...;",\n'
                    '      "reason": "why this helps or should be dropped",\n'
                    '      "confidence": 0.0,\n'
                    '      "index_type": "btree"\n'
                    "    }\n"
                    "  ]\n"
                    "}\n\n"
                    f"Payload:\n{json.dumps(payload, ensure_ascii=True, indent=2)}"
                ),
            },
        ]

    def _recommendations_from_response(
        self,
        response_text: str,
        structure: list[dict],
        current_indices: list[dict],
    ) -> list[IndexRecommendation]:
        payload = self._extract_json_object(response_text)
        table_names = {str(table.get("name", "")) for table in structure}
        index_names = {str(index.get("name", "")) for index in current_indices}
        recommendations: list[IndexRecommendation] = []
        for item in payload.get("recommendations", []):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", "")).strip().lower()
            table = self._identifier(str(item.get("table", "")))
            columns = [self._identifier(str(column)) for column in item.get("columns", []) if str(column).strip()]
            name = self._identifier(str(item.get("name", "")))
            sql_text = self._normalize_index_sql(str(item.get("sql", "")))
            if action not in {"create", "drop"} or not sql_text:
                continue
            if action == "create" and table not in table_names:
                continue
            if action == "drop" and name and index_names and name not in index_names:
                continue
            if action == "create" and (not table or not columns):
                continue
            recommendations.append(
                IndexRecommendation(
                    action=action,
                    table=table,
                    columns=columns,
                    name=name or self._index_name(table, columns),
                    sql=sql_text,
                    reason=str(item.get("reason", "")),
                    confidence=self._confidence(item.get("confidence", 0.5)),
                    index_type=str(item.get("index_type", "btree") or "btree"),
                )
            )
        return recommendations

    def _normalize_index_sql(self, sql_text: str) -> str:
        statement = sql_text.strip().rstrip(";")
        if not statement:
            return ""
        if re.search(r"\bconcurrently\b", statement, re.IGNORECASE):
            return ""
        if not re.match(r"^\s*(create\s+index|drop\s+index)\b", statement, re.IGNORECASE):
            return ""
        if ";" in statement:
            return ""
        return statement + ";"

    def _extract_json_object(self, text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain a JSON object.")
        return json.loads(match.group(0))

    def _structure_payload(self, schema: SchemaSnapshot | None, target_tables: list[dict]) -> list[dict]:
        if schema is not None and schema.tables:
            return [
                {
                    "name": self._identifier(table.name),
                    "schema": table.schema,
                    "columns": [
                        {
                            "name": self._identifier(column.name),
                            "data_type": column.data_type,
                            "is_nullable": column.is_nullable,
                        }
                        for column in table.columns
                    ],
                    "unique_constraints": [constraint.columns for constraint in table.unique_constraints],
                    "foreign_keys": [
                        {
                            "columns": foreign_key.columns,
                            "references_table": foreign_key.referenced_table.split(".")[-1],
                            "references_columns": foreign_key.referenced_columns,
                        }
                        for foreign_key in table.foreign_keys
                    ],
                }
                for table in schema.tables
            ]
        return [
            {
                "name": self._identifier(str(table.get("name", ""))),
                "schema": "",
                "columns": [
                    {"name": self._identifier(str(column.get("name", ""))), "data_type": column.get("data_type", "")}
                    for column in table.get("columns", [])
                    if str(column.get("name", "")).strip()
                ],
                "unique_constraints": table.get("uniques", []),
                "foreign_keys": table.get("foreign_keys", []),
            }
            for table in target_tables
            if str(table.get("name", "")).strip()
        ]

    def _current_index_payload(self, schema: SchemaSnapshot | None) -> list[dict]:
        if schema is None:
            return []
        indexes: list[dict] = []
        unique_index_names = {
            constraint.name
            for table in schema.tables
            for constraint in table.unique_constraints
        }
        for table in schema.tables:
            for constraint in table.unique_constraints:
                indexes.append(
                    {
                        "name": constraint.name,
                        "table": self._identifier(table.name),
                        "columns": constraint.columns,
                        "is_unique": True,
                        "is_constraint": True,
                        "definition": "",
                    }
                )
            for index in table.indexes:
                indexes.append(
                    {
                        "name": index.name,
                        "table": self._identifier(table.name),
                        "columns": self._columns_from_index_definition(index.definition),
                        "is_unique": index.is_unique,
                        "is_constraint": index.name in unique_index_names,
                        "definition": index.definition,
                    }
                )
        return indexes

    def _columns_from_index_definition(self, definition: str) -> list[str]:
        match = re.search(r"\((?P<columns>[^)]+)\)", definition)
        if not match:
            return []
        return [self._identifier(item.strip().strip('"')) for item in match.group("columns").split(",")]

    def _chunks(self, values: list, size: int) -> list[list]:
        return [values[index : index + size] for index in range(0, len(values), size)]

    def _dedupe_recommendations(self, recommendations: list[IndexRecommendation]) -> list[IndexRecommendation]:
        seen: set[str] = set()
        result: list[IndexRecommendation] = []
        for recommendation in recommendations:
            key = self._normalize_sql(recommendation.sql)
            if key in seen:
                continue
            seen.add(key)
            result.append(recommendation)
        return result

    def _confidence(self, value) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.5

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
