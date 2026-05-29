from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import LogSummary


LINE_COMMENT_RE = re.compile(r"--.*?$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
PGBENCH_META_COMMAND_RE = re.compile(r"^\s*\\.*$", re.MULTILINE)


@dataclass(slots=True)
class WorkloadPlan:
    status: str
    summary: str
    candidate_summary_tables: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class WorkloadRewriteProposal:
    status: str
    summary: str
    rationale: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    source: str = "fallback"
    raw_response_text: str | None = None


class WorkloadRewriteError(ValueError):
    def __init__(self, message: str, raw_response_text: str = "") -> None:
        super().__init__(message)
        self.raw_response_text = raw_response_text


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
        candidates = self._summary_table_candidates(log_summary)
        if candidates:
            return WorkloadPlan(
                status="recommended",
                summary=f"Found {len(candidates)} candidate summary-table pattern(s) in the workload.",
                candidate_summary_tables=candidates,
            )
        return WorkloadPlan(
            status="planned",
            summary=(
                "Workload extraction is active. No obvious aggregate summary-table candidate was detected yet."
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

        if len(normalized_source_statements) > 1:
            return self._rewrite_statement_batches(
                normalized_source_statements,
                target_tables,
                migration_statements,
            )
        return self._rewrite_statement_batch(normalized_source_statements, target_tables, migration_statements)

    def _rewrite_statement_batches(
        self,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
    ) -> WorkloadRewriteProposal:
        statements: list[str] = []
        reasoning: list[str] = []
        raw_responses: list[str] = []
        sources: list[str] = []
        for index, statement in enumerate(source_statements, start=1):
            proposal = self._rewrite_statement_batch([statement], target_tables, migration_statements, batch_index=index)
            statements.extend(proposal.statements)
            reasoning.extend(proposal.rationale)
            sources.append(proposal.source)
            if proposal.raw_response_text:
                raw_responses.append(f"Batch {index}:\n{proposal.raw_response_text}")
        return WorkloadRewriteProposal(
            status="planned",
            summary=f"Rewrote {len(statements)} query statement(s) for db-new in {len(source_statements)} model call(s).",
            rationale=reasoning,
            statements=statements,
            source="llm_batched" if any(source == "llm" for source in sources) else "llm_batched_fallback",
            raw_response_text="\n\n".join(raw_responses),
        )

    def _rewrite_statement_batch(
        self,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
        batch_index: int | None = None,
    ) -> WorkloadRewriteProposal:
        messages = self._build_rewrite_messages(source_statements, target_tables, migration_statements, batch_index=batch_index)
        try:
            llm_result = self._llm.generate(messages, max_tokens=2400)
        except Exception as exc:
            raise WorkloadRewriteError(
                f"LLM request failed while rewriting query batch {batch_index or 1}: {exc}"
            ) from exc
        try:
            proposal = self._proposal_from_response(llm_result.response_text, target_tables)
            proposal.raw_response_text = llm_result.response_text
            return proposal
        except ValueError as first_error:
            direct_sql_proposal = self._proposal_from_direct_sql(llm_result.response_text, target_tables)
            if direct_sql_proposal is not None:
                return direct_sql_proposal

            repair_messages = self._build_repair_messages(
                source_statements,
                target_tables,
                migration_statements,
                llm_result.response_text,
                str(first_error),
            )
            try:
                repair_result = self._llm.generate(repair_messages, max_tokens=2400)
            except Exception as exc:
                raise WorkloadRewriteError(
                    f"LLM repair request failed while rewriting query batch {batch_index or 1}: {exc}",
                    raw_response_text=llm_result.response_text,
                ) from exc
            raw_response_text = (
                "Initial response:\n"
                f"{llm_result.response_text}\n\n"
                "Repair response:\n"
                f"{repair_result.response_text}"
            )
            try:
                proposal = self._proposal_from_response(repair_result.response_text, target_tables)
                proposal.raw_response_text = raw_response_text
                return proposal
            except ValueError as repair_error:
                direct_sql_proposal = self._proposal_from_direct_sql(repair_result.response_text, target_tables)
                if direct_sql_proposal is not None:
                    direct_sql_proposal.raw_response_text = raw_response_text
                    return direct_sql_proposal
                raise WorkloadRewriteError(
                    (
                        "The LLM did not return valid JSON or executable SQL for query migration after a retry. "
                        f"Last parser error: {repair_error}"
                    ),
                    raw_response_text=raw_response_text,
                ) from repair_error

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

    def _proposal_from_direct_sql(self, response_text: str, target_tables: list[dict]) -> WorkloadRewriteProposal | None:
        target_table_names = {str(table.get("name", "")).strip() for table in target_tables if str(table.get("name", "")).strip()}
        try:
            statements = [
                self._normalize_statement(statement, target_table_names)
                for statement in self._split_statements(response_text)
            ]
        except ValueError:
            return None
        statements = [statement for statement in statements if statement]
        if not statements:
            return None
        return WorkloadRewriteProposal(
            status="planned",
            summary="The LLM returned SQL without a JSON wrapper; IlvesBench recovered executable statements.",
            rationale=["Recovered direct SQL response after JSON parsing failed."],
            statements=statements,
            source="llm_sql_fallback",
            raw_response_text=response_text,
        )

    def _build_rewrite_messages(
        self,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
        batch_index: int | None = None,
    ) -> list[dict[str, str]]:
        system_prompt = (
            "You rewrite SQL workloads from an original PostgreSQL schema to a normalized target schema. "
            "Return JSON only and keep each rewritten query logically equivalent to the source query."
        )
        user_prompt = (
            (
                f"Rewrite workload query batch {batch_index} so it targets db-new instead of db-original.\n\n"
                if batch_index is not None
                else "Rewrite the source workload so it targets db-new instead of db-original.\n\n"
            )
            + "Rules:\n"
            "1. Return one rewritten statement for each source statement, in the same order.\n"
            "2. Preserve the SQL operation type when possible so the rewritten workload remains benchmarkable.\n"
            "3. Use the normalized target-table names exactly as given.\n"
            "4. Preserve the filtering intent, joins, projected information, and operand data types as closely as possible.\n"
            "5. Do not compare varchar/text columns to bare numeric literals; use a matching literal type or an explicit cast if needed.\n"
            "6. Do not prefix table names with database names like db-new, db_new, or the target database name. Use plain table names or public.table only.\n"
            "7. WITH queries are allowed when they lead to SELECT/INSERT/UPDATE/DELETE.\n"
            "8. Do not include markdown fences, comments, EXPLAIN, CREATE, ALTER, DROP, TRUNCATE, GRANT, or REVOKE statements.\n"
            "9. Return JSON only.\n\n"
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

    def _build_repair_messages(
        self,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
        invalid_response: str,
        parser_error: str,
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You repair malformed query-migration output. Return one JSON object only. "
                    "No prose, no markdown, no code fences."
                ),
            },
            {
                "role": "user",
                "content": (
                    "The previous answer could not be parsed by IlvesBench.\n\n"
                    f"Parser error:\n{parser_error}\n\n"
                    "Convert the previous answer into this exact JSON shape:\n"
                    "{\n"
                    '  "status": "planned",\n'
                    '  "summary": "short summary",\n'
                    '  "reasoning": ["..."],\n'
                    '  "statements": ["SELECT ...", "WITH ... SELECT ..."]\n'
                    "}\n\n"
                    "Rules:\n"
                    "1. Include exactly one rewritten SQL statement per source statement when possible.\n"
                    "2. Statements may start with WITH, SELECT, INSERT, UPDATE, or DELETE only.\n"
                    "3. Do not include comments, markdown, EXPLAIN, CREATE, ALTER, DROP, TRUNCATE, GRANT, or REVOKE.\n"
                    "4. Use target table names exactly as given.\n\n"
                    f"Source workload statements:\n{json.dumps(source_statements, ensure_ascii=True, indent=2)}\n\n"
                    f"Target tables:\n{json.dumps(target_tables, ensure_ascii=True, indent=2)}\n\n"
                    f"Migration statements:\n{json.dumps(migration_statements, ensure_ascii=True, indent=2)}\n\n"
                    f"Previous invalid response:\n{invalid_response}"
                ),
            },
        ]

    def _extract_json_object(self, text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain a JSON object.")
        return json.loads(match.group(0))

    def _split_statements(self, text: str) -> list[str]:
        text = self._preprocess_workload_sql(text)
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

    def _preprocess_workload_sql(self, text: str) -> str:
        without_block_comments = BLOCK_COMMENT_RE.sub("", text)
        without_line_comments = LINE_COMMENT_RE.sub("", without_block_comments)
        return PGBENCH_META_COMMAND_RE.sub("", without_line_comments)

    def _summary_table_candidates(self, log_summary: LogSummary) -> list[dict]:
        candidates: list[dict] = []
        for index, query in enumerate(log_summary.top_queries, start=1):
            sql = query.sample_sql.strip()
            normalized = re.sub(r"\s+", " ", sql, flags=re.MULTILINE).strip()
            if not normalized:
                continue
            has_aggregate = bool(re.search(r"\b(count|sum|avg|min|max)\s*\(", normalized, re.IGNORECASE))
            group_by = re.search(r"\bgroup\s+by\s+(?P<columns>.+?)(?:\border\s+by\b|\blimit\b|$)", normalized, re.IGNORECASE)
            if not has_aggregate or not group_by:
                continue
            group_columns = [
                item.strip().strip('"')
                for item in group_by.group("columns").rstrip(";").split(",")
                if item.strip()
            ][:6]
            candidates.append(
                {
                    "label": f"Summary candidate {len(candidates) + 1}",
                    "pattern": "aggregate_group_by",
                    "query_frequency": query.count,
                    "grouping_column_count": len(group_columns),
                    "grouping_columns_sample": group_columns,
                    "reason": "Repeated aggregate query with GROUP BY could be materialized or maintained as a summary table.",
                    "creation_status": "placeholder",
                    "sample_query": normalized[:500],
                }
            )
            if len(candidates) >= 8:
                break
        return candidates

    def _normalize_statement(self, sql: str, target_table_names: set[str]) -> str:
        normalized = sql.strip().rstrip(";")
        if not normalized:
            return ""
        normalized = self._strip_database_qualifiers(normalized, target_table_names)
        if not re.match(r"^(WITH|SELECT|INSERT|UPDATE|DELETE)\b", normalized, re.IGNORECASE):
            raise ValueError("Rewritten workload must contain WITH, SELECT, INSERT, UPDATE, or DELETE statements only.")
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
