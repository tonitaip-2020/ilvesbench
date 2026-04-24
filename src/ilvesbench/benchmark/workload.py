from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import LogSummary


@dataclass(slots=True)
class WorkloadPlan:
    status: str
    summary: str
    candidate_summary_tables: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WorkloadRewriteProposal:
    status: str
    summary: str
    rationale: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    source: str = "fallback"
    raw_response_text: str | None = None


class WorkloadPlanner:
    """Workload extraction, rewrite planning, and summary-table placeholders."""

    def __init__(self, llm: LLMGateway | None = None, target_database: str | None = None) -> None:
        self._llm = llm
        self._target_database = target_database

    def build_plan(self, log_summary: LogSummary | None) -> WorkloadPlan:
        if log_summary is None:
            return WorkloadPlan(
                status="input_required",
                summary="No workload source was available. Provide a PostgreSQL log file or a workload SQL file.",
            )
        if not log_summary.top_queries:
            return WorkloadPlan(
                status="no_queries",
                summary="The workload source was parsed, but no executable SQL statements were detected.",
            )
        return WorkloadPlan(
            status="planned",
            summary=(
                "Workload extraction is active, and query transformation can now be proposed for db-new. "
                "Summary-table synthesis remains a later module."
            ),
        )

    def rewrite(
        self,
        workload_sql: str,
        target_tables: list[dict],
        migration_statements: list[dict],
    ) -> WorkloadRewriteProposal:
        normalized_source_statements = self._split_statements(workload_sql)
        if not normalized_source_statements:
            return WorkloadRewriteProposal(
                status="no_queries",
                summary="No workload queries were available to rewrite for db-new.",
                source="fallback",
            )
        if not target_tables:
            return WorkloadRewriteProposal(
                status="no_rewrite_needed",
                summary="No normalized target tables were proposed, so no db-new workload rewrite was generated.",
                statements=normalized_source_statements,
                source="fallback",
            )
        if self._llm is None:
            return WorkloadRewriteProposal(
                status="unavailable",
                summary="Workload rewriting is unavailable because no LLM gateway is configured.",
                source="fallback",
            )

        llm_result = self._llm.generate(
            self._build_rewrite_messages(normalized_source_statements, target_tables, migration_statements),
            max_tokens=2400,
        )
        proposal = self._proposal_from_response(llm_result.response_text, target_tables)
        proposal.raw_response_text = llm_result.response_text
        return proposal

    def _proposal_from_response(self, response_text: str, target_tables: list[dict]) -> WorkloadRewriteProposal:
        payload = self._extract_json_object(response_text)
        statements = []
        target_table_names = {str(table.get("name", "")).strip() for table in target_tables if str(table.get("name", "")).strip()}
        for item in payload.get("statements", []):
            sql = str(item).strip()
            normalized = self._normalize_statement(sql, target_table_names)
            if normalized:
                statements.append(normalized)
        if not statements:
            raise ValueError("Workload rewrite response did not contain any valid SQL statements.")
        return WorkloadRewriteProposal(
            status=str(payload.get("status", "planned")),
            summary=str(payload.get("summary", "Workload was rewritten for db-new.")),
            rationale=[str(item) for item in payload.get("reasoning", [])],
            statements=statements,
            source="llm",
        )

    def _build_rewrite_messages(
        self,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
    ) -> list[dict[str, str]]:
        system_prompt = (
            "You rewrite SQL workloads from an original PostgreSQL schema to a normalized target schema. "
            "Return JSON only and keep each rewritten query logically equivalent to the source query."
        )
        user_prompt = (
            "Rewrite the source workload so it targets db-new instead of db-original.\n\n"
            "Rules:\n"
            "1. Return one rewritten statement for each source statement, in the same order.\n"
            "2. Preserve the SQL operation type when possible so the rewritten workload remains benchmarkable.\n"
            "3. Use the normalized target-table names exactly as given.\n"
            "4. Preserve the filtering intent, joins, projected information, and operand data types as closely as possible.\n"
            "5. Do not compare varchar/text columns to bare numeric literals; use a matching literal type or an explicit cast if needed.\n"
            "6. Do not prefix table names with database names like db-new, db_new, or the target database name. Use plain table names or public.table only.\n"
            "7. Do not include markdown fences, comments, EXPLAIN, CREATE, ALTER, DROP, TRUNCATE, GRANT, or REVOKE statements.\n"
            "8. Return JSON only.\n\n"
            "Return JSON with this shape:\n"
            "{\n"
            '  "status": "planned|no_rewrite_needed",\n'
            '  "summary": "short summary",\n'
            '  "reasoning": ["..."],\n'
            '  "statements": ["SELECT ...", "SELECT ..."]\n'
            "}\n\n"
            f"Source workload statements:\n{json.dumps(source_statements, ensure_ascii=True, indent=2)}\n\n"
            f"Target tables:\n{json.dumps(target_tables, ensure_ascii=True, indent=2)}\n\n"
            f"Migration statements:\n{json.dumps(migration_statements, ensure_ascii=True, indent=2)}"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _extract_json_object(self, text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain a JSON object.")
        return json.loads(match.group(0))

    def _split_statements(self, text: str) -> list[str]:
        statements: list[str] = []
        current: list[str] = []
        in_single = False
        in_double = False

        for char in text:
            if char == "'" and not in_double:
                in_single = not in_single
            elif char == '"' and not in_single:
                in_double = not in_double

            if char == ";" and not in_single and not in_double:
                statement = self._normalize_statement("".join(current), set())
                if statement:
                    statements.append(statement)
                current = []
                continue

            current.append(char)

        tail = self._normalize_statement("".join(current), set())
        if tail:
            statements.append(tail)
        return statements

    def _normalize_statement(self, sql: str, target_table_names: set[str]) -> str:
        normalized = sql.strip().rstrip(";")
        if not normalized:
            return ""
        normalized = self._strip_database_qualifiers(normalized, target_table_names)
        if not re.match(r"^(SELECT|INSERT|UPDATE|DELETE)\b", normalized, re.IGNORECASE):
            raise ValueError("Rewritten workload must contain SELECT, INSERT, UPDATE, or DELETE statements only.")
        return normalized + ";"

    def _strip_database_qualifiers(self, sql: str, target_table_names: set[str]) -> str:
        qualifiers = {"db-new", "db_new", "new_db", "dbnew"}
        if self._target_database:
            qualifiers.add(self._target_database)

        for table_name in sorted(target_table_names, key=len, reverse=True):
            table_pattern = re.escape(table_name)
            for qualifier in qualifiers:
                qualifier_pattern = re.escape(qualifier)
                sql = re.sub(
                    rf'(?i)(?<![A-Za-z0-9_])(?:"{qualifier_pattern}"|{qualifier_pattern})\.(?:"{table_pattern}"|{table_pattern})\b',
                    table_name,
                    sql,
                )
        return sql
