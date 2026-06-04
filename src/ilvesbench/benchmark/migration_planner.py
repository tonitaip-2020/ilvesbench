from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import SchemaSnapshot


@dataclass(slots=True)
class MigrationProposal:
    status: str
    summary: str
    rationale: list[str] = field(default_factory=list)
    statements: list[dict] = field(default_factory=list)
    source: str = "unavailable"
    raw_response_text: str | None = None
    request_payload: dict | None = None


class MigrationPlanner:
    def __init__(self, llm: LLMGateway | None = None) -> None:
        self._llm = llm

    def plan(self, schema: SchemaSnapshot | None, target_tables: list[dict]) -> MigrationProposal:
        if not target_tables:
            return MigrationProposal(
                status="no_migration_needed",
                summary="No migration plan was generated because no normalized target tables were proposed.",
                rationale=["Migration planning requires a target-table decomposition."],
                source="fallback",
            )
        deterministic = self._deterministic_first_normal_form_plan(schema, target_tables)
        if deterministic is not None:
            deterministic.request_payload = self._request_payload(schema, target_tables)
            return deterministic
        if self._llm is None:
            return MigrationProposal(
                status="unavailable",
                summary="Migration planning is unavailable because no LLM gateway is configured.",
                source="fallback",
            )

        request_payload = self._request_payload(schema, target_tables)
        llm_result = self._llm.generate(self._build_messages(schema, target_tables), max_tokens=1800)
        proposal = self._proposal_from_response(llm_result.response_text, target_tables)
        proposal.raw_response_text = llm_result.response_text
        proposal.request_payload = request_payload
        return proposal

    def _request_payload(self, schema: SchemaSnapshot | None, target_tables: list[dict]) -> dict:
        return {
            "source_tables": [
                {
                    "table": f"{table.schema}.{table.name}",
                    "columns": [column.name for column in table.columns],
                }
                for table in (schema.tables if schema is not None else [])
            ],
            "target_tables": target_tables,
        }

    def _proposal_from_response(self, response_text: str, target_tables: list[dict]) -> MigrationProposal:
        payload = self._extract_json_object(response_text)
        statements: list[dict] = []
        target_table_map = {table["name"]: table for table in target_tables}
        for item in payload.get("statements", []):
            if not isinstance(item, dict):
                continue
            target_table = self._sanitize_identifier(str(item.get("target_table", "")).strip())
            sql = str(item.get("sql", "")).strip()
            purpose = str(item.get("purpose", "")).strip()
            if not target_table or target_table not in target_table_map:
                raise ValueError(f"Migration plan references unknown target table: {target_table}")
            normalized_sql = self._normalize_sql(sql, target_table_map[target_table])
            if not self._is_allowed_statement(normalized_sql):
                raise ValueError("Migration plan must contain INSERT ... SELECT statements only.")
            statements.append(
                {
                    "target_table": target_table,
                    "sql": normalized_sql,
                    "purpose": purpose,
                }
            )

        return MigrationProposal(
            status=str(payload.get("status", "planned")),
            summary=str(payload.get("summary", "Migration statements were generated.")),
            rationale=[str(item) for item in payload.get("reasoning", [])],
            statements=statements,
            source="llm",
        )

    def _build_messages(self, schema: SchemaSnapshot, target_tables: list[dict]) -> list[dict[str, str]]:
        source_tables = [
            {
                "table": f"{table.schema}.{table.name}",
                "columns": [column.name for column in table.columns],
            }
            for table in schema.tables
        ]
        system_prompt = (
            "You are a SQL migration planner. Return JSON only. "
            "Generate INSERT INTO ... SELECT statements that populate normalized target tables."
        )
        user_prompt = (
            "Plan data migration from the source schema to the normalized target tables.\n\n"
            "Rules:\n"
            "1. Use target tables exactly as named.\n"
            "2. Read source data from foreign tables under the schema placeholder __SOURCE_SCHEMA__.\n"
            "3. Every statement must be an INSERT INTO ... SELECT statement.\n"
            "4. Use DISTINCT where needed to populate lookup tables.\n"
            "5. Do not emit DELETE, UPDATE, TRUNCATE, DROP, ALTER, or CREATE statements.\n"
            "6. Return JSON only.\n\n"
            "Return JSON with this shape:\n"
            "{\n"
            '  "status": "planned|no_migration_needed",\n'
            '  "summary": "short summary",\n'
            '  "reasoning": ["..."],\n'
            '  "statements": [\n'
            "    {\n"
            '      "target_table": "table_name",\n'
            '      "purpose": "what this inserts",\n'
            '      "sql": "INSERT INTO ... SELECT ..."\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"Source tables:\n{json.dumps(source_tables, ensure_ascii=True, indent=2)}\n\n"
            f"Target tables:\n{json.dumps(target_tables, ensure_ascii=True, indent=2)}"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _deterministic_first_normal_form_plan(self, schema: SchemaSnapshot | None, target_tables: list[dict]) -> MigrationProposal | None:
        if not any(table.get("migration_strategy") for table in target_tables):
            return None

        statements: list[dict] = []
        copy_tables = [table for table in target_tables if table.get("migration_strategy") == "copy_distinct"]
        split_tables = [table for table in target_tables if table.get("migration_strategy") == "split_delimited"]
        for table in copy_tables:
            source_table = self._single_source_table(table)
            if not source_table:
                continue
            target_columns = [str(column.get("name", "")) for column in table.get("columns", []) if column.get("name")]
            source_columns = [
                self._source_column_name(schema, source_table, str(column.get("source_column", "")))
                for column in table.get("columns", [])
                if column.get("source_column")
            ]
            if not target_columns or len(target_columns) != len(source_columns):
                continue
            target_column_sql = ", ".join(self._quote_identifier(column) for column in target_columns)
            source_column_sql = ", ".join(self._quote_identifier(column) for column in source_columns)
            statements.append(
                {
                    "target_table": table["name"],
                    "purpose": f"Copy source rows from {source_table}.",
                    "sql": (
                        f"INSERT INTO {self._quote_identifier(table['name'])} ({target_column_sql}) "
                        f"SELECT DISTINCT {source_column_sql} "
                        f"FROM __SOURCE_SCHEMA__.{self._quote_identifier(self._source_table_name(source_table))};"
                    ),
                }
            )

        for table in split_tables:
            source_table = self._single_source_table(table)
            source_column = str(table.get("split_source_column", "")).strip()
            value_column = str(table.get("split_value_column", "")).strip()
            delimiter = str(table.get("split_delimiter", ","))
            if not source_table or not source_column or not value_column:
                continue
            target_columns = [str(column.get("name", "")) for column in table.get("columns", []) if column.get("name")]
            parent_columns = [column for column in target_columns if column != value_column]
            if not parent_columns:
                continue
            target_column_sql = ", ".join(self._quote_identifier(column) for column in target_columns)
            selected_columns = ", ".join(
                [self._quote_identifier(column) for column in parent_columns] + ["trim(extracted_value)"]
            )
            resolved_source_column = self._source_column_name(schema, source_table, source_column)
            source_column_sql = self._quote_identifier(resolved_source_column)
            statements.append(
                {
                    "target_table": table["name"],
                    "purpose": f"Split delimited values from {source_table}.{source_column}.",
                    "sql": (
                        f"INSERT INTO {self._quote_identifier(table['name'])} ({target_column_sql}) "
                        f"SELECT DISTINCT {selected_columns} "
                        f"FROM __SOURCE_SCHEMA__.{self._quote_identifier(self._source_table_name(source_table))} "
                        f"CROSS JOIN LATERAL unnest(string_to_array({source_column_sql}, {self._sql_literal(delimiter)})) AS extracted_value "
                        f"WHERE {source_column_sql} IS NOT NULL AND trim(extracted_value) <> '';"
                    ),
                }
            )

        if not statements:
            return None
        return MigrationProposal(
            status="planned",
            summary=f"Generated {len(statements)} deterministic migration statement(s) for 1NF decomposition.",
            rationale=[
                "Copied regular tables from the source database.",
                "Populated child tables by splitting detected delimited multi-value columns.",
            ],
            statements=statements,
            source="deterministic_1nf",
        )

    def _single_source_table(self, target_table: dict) -> str:
        source_tables = [str(item) for item in target_table.get("source_tables", []) if str(item).strip()]
        return source_tables[0] if len(source_tables) == 1 else ""

    def _source_table_name(self, source_table: str) -> str:
        return source_table.split(".")[-1]

    def _source_column_name(self, schema: SchemaSnapshot | None, source_table: str, source_column: str) -> str:
        source_column = source_column.strip()
        if not source_column:
            return ""
        if schema is None:
            return source_column
        schema_name = source_table.split(".")[0] if "." in source_table else ""
        table_name = self._source_table_name(source_table)
        normalized = self._sanitize_identifier(source_column)
        for table in schema.tables:
            if table.name.lower() != table_name.lower():
                continue
            if schema_name and table.schema.lower() != schema_name.lower():
                continue
            for column in table.columns:
                if column.name == source_column:
                    return column.name
            for column in table.columns:
                if column.name.lower() == source_column.lower() or self._sanitize_identifier(column.name) == normalized:
                    return column.name
        return source_column

    def _sql_literal(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _quote_identifier(self, value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'

    def _extract_json_object(self, text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain a JSON object.")
        return json.loads(match.group(0))

    def _normalize_sql(self, sql: str, target_table: dict) -> str:
        sql = sql.strip().rstrip(";")
        sql = self._normalize_source_references(sql)
        sql = self._normalize_insert_target(sql, target_table)
        return sql + ";"

    def _is_allowed_statement(self, sql: str) -> bool:
        normalized = re.sub(r"\s+", " ", sql.strip()).upper()
        return normalized.startswith("INSERT INTO ") and " SELECT " in normalized

    def _sanitize_identifier(self, value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()

    def _normalize_source_references(self, sql: str) -> str:
        sql = re.sub(r'("__SOURCE_SCHEMA__"|__SOURCE_SCHEMA__)\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)', r'__SOURCE_SCHEMA__.\3', sql)
        return sql.replace('"__SOURCE_SCHEMA__"', "__SOURCE_SCHEMA__")

    def _normalize_insert_target(self, sql: str, target_table: dict) -> str:
        match = re.match(
            r'^\s*INSERT\s+INTO\s+("?)(?P<table>[A-Za-z0-9_]+)\1\s*\((?P<columns>[^)]*)\)',
            sql,
            re.IGNORECASE,
        )
        if not match:
            raise ValueError("Migration plan must start with INSERT INTO <table> (<columns>).")

        target_table_name = target_table["name"]
        if self._sanitize_identifier(match.group("table")) != target_table_name:
            raise ValueError(
                f"Migration statement targets {match.group('table')} but the approved table is {target_table_name}."
            )

        requested_columns = [
            self._sanitize_identifier(column.strip().strip('"'))
            for column in match.group("columns").split(",")
            if column.strip()
        ]
        valid_columns = {column["name"] for column in target_table.get("columns", [])}
        invalid_columns = [column for column in requested_columns if column not in valid_columns]
        if invalid_columns:
            raise ValueError(
                f"Migration statement for {target_table_name} uses columns not present in the approved target table: "
                f"{', '.join(invalid_columns)}."
            )

        canonical_columns = ", ".join(requested_columns)
        return re.sub(
            r'^\s*INSERT\s+INTO\s+("?)([A-Za-z0-9_]+)\1\s*\(([^)]*)\)',
            f'INSERT INTO {target_table_name} ({canonical_columns})',
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
