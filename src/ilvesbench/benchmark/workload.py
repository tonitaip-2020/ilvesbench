from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import LogSummary


LINE_COMMENT_RE = re.compile(r"--.*?$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
PGBENCH_META_COMMAND_RE = re.compile(r"^\s*\\.*$", re.MULTILINE)
MARKDOWN_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*$|^\s*```\s*$", re.IGNORECASE | re.MULTILINE)
DEFAULT_REWRITE_BATCH_SIZE = 25


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
    request_payload: dict | None = None


class WorkloadRewriteError(ValueError):
    def __init__(
        self,
        message: str,
        raw_response_text: str = "",
        request_payload: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_response_text = raw_response_text
        self.request_payload = request_payload or {}


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
        *,
        batch_size: int = DEFAULT_REWRITE_BATCH_SIZE,
        source_tables: list[dict] | None = None,
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

        if len(normalized_source_statements) > batch_size:
            return self._rewrite_statement_batches(
                normalized_source_statements,
                target_tables,
                migration_statements,
                batch_size=batch_size,
                source_tables=source_tables or [],
            )
        return self._rewrite_statement_batch(
            normalized_source_statements,
            target_tables,
            migration_statements,
            source_tables=source_tables or [],
        )

    def source_statements(self, workload_sql: str) -> list[str]:
        return self._split_statements(workload_sql)

    def rewrite_one(
        self,
        source_statement: str,
        target_tables: list[dict],
        migration_statements: list[dict],
        *,
        query_index: int,
        source_tables: list[dict] | None = None,
    ) -> WorkloadRewriteProposal:
        if self._llm is None:
            return WorkloadRewriteProposal(
                status="unavailable",
                summary="Workload rewriting is unavailable because no LLM gateway is configured.",
                source="fallback",
            )
        normalized = self._split_statements(source_statement)
        if not normalized:
            return WorkloadRewriteProposal(
                status="no_queries",
                summary="No workload query was available to rewrite.",
                source="fallback",
            )
        return self._rewrite_statement_batch(
            [normalized[0]],
            target_tables,
            migration_statements,
            batch_index=query_index,
            source_tables=source_tables or [],
        )

    def repair_rewrite(
        self,
        source_statement: str,
        previous_rewrite: str,
        validation_error: dict,
        target_tables: list[dict],
        migration_statements: list[dict],
        *,
        query_index: int,
        source_tables: list[dict] | None = None,
    ) -> WorkloadRewriteProposal:
        if self._llm is None:
            return WorkloadRewriteProposal(
                status="unavailable",
                summary="Query rewrite repair is unavailable because no LLM gateway is configured.",
                source="fallback",
            )
        source_tables = source_tables or []
        request_payload = {
            "mode": "validation_repair",
            "query_index": query_index,
            "source_statement": source_statement,
            "previous_rewrite": previous_rewrite,
            "validation_error": validation_error,
            "source_tables": source_tables,
            "target_tables": target_tables,
            "migration_statements": migration_statements,
        }
        messages = self._build_validation_repair_messages(
            source_statement,
            previous_rewrite,
            validation_error,
            target_tables,
            migration_statements,
            query_index=query_index,
            source_tables=source_tables,
        )
        try:
            llm_result = self._llm.generate(messages, max_tokens=4096)
        except Exception as exc:
            raise WorkloadRewriteError(
                f"LLM request failed while repairing rewritten query {query_index}: {exc}",
                request_payload=request_payload,
            ) from exc
        proposal = self._proposal_from_direct_sql(llm_result.response_text, target_tables)
        if proposal is None and llm_result.response_text.lstrip().startswith("{"):
            try:
                proposal = self._proposal_from_response(llm_result.response_text, target_tables)
            except ValueError:
                proposal = None
        if proposal is None:
            raise WorkloadRewriteError(
                "LLM response did not contain executable repaired SQL.",
                raw_response_text=llm_result.response_text,
                request_payload=request_payload,
            )
        proposal.raw_response_text = llm_result.response_text
        proposal.request_payload = request_payload
        return proposal

    def _rewrite_statement_batches(
        self,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
        *,
        batch_size: int = DEFAULT_REWRITE_BATCH_SIZE,
        source_tables: list[dict] | None = None,
    ) -> WorkloadRewriteProposal:
        statements: list[str] = []
        reasoning: list[str] = []
        raw_responses: list[str] = []
        request_batches: list[dict] = []
        sources: list[str] = []
        chunks = [
            source_statements[index : index + batch_size]
            for index in range(0, len(source_statements), batch_size)
        ]
        for index, chunk in enumerate(chunks, start=1):
            try:
                proposal = self._rewrite_statement_batch(
                    chunk,
                    target_tables,
                    migration_statements,
                    batch_index=index,
                    source_tables=source_tables or [],
                )
            except WorkloadRewriteError as exc:
                request_batches.extend([exc.request_payload] if exc.request_payload else [])
                combined_raw = "\n\n".join(
                    raw_responses
                    + ([f"Batch {index}:\n{exc.raw_response_text}"] if exc.raw_response_text else [])
                )
                raise WorkloadRewriteError(
                    str(exc),
                    raw_response_text=combined_raw,
                    request_payload={
                        "mode": "batched",
                        "batch_count": len(source_statements),
                        "failed_batch": index,
                        "batches": request_batches,
                    },
                ) from exc
            statements.extend(proposal.statements)
            reasoning.extend(proposal.rationale)
            sources.append(proposal.source)
            if proposal.request_payload:
                request_batches.append(proposal.request_payload)
            if proposal.raw_response_text:
                raw_responses.append(f"Batch {index}:\n{proposal.raw_response_text}")
        return WorkloadRewriteProposal(
            status="planned",
            summary=f"Rewrote {len(statements)} query statement(s) for db-new in {len(chunks)} model call(s).",
            rationale=reasoning,
            statements=statements,
            source="llm_batched" if any(source == "llm" for source in sources) else "llm_batched_fallback",
            raw_response_text="\n\n".join(raw_responses),
            request_payload={
                "mode": "batched",
                "batch_count": len(chunks),
                "batch_size": batch_size,
                "batches": request_batches,
            },
        )

    def _rewrite_statement_batch(
        self,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
        batch_index: int | None = None,
        source_tables: list[dict] | None = None,
    ) -> WorkloadRewriteProposal:
        source_tables = source_tables or []
        request_payload = {
            "mode": "single_query" if batch_index is not None else "all_queries",
            "batch_index": batch_index,
            "source_statements": source_statements,
            "source_tables": source_tables,
            "target_tables": target_tables,
            "migration_statements": migration_statements,
        }
        messages = self._build_rewrite_messages(
            source_statements,
            target_tables,
            migration_statements,
            batch_index=batch_index,
            source_tables=source_tables,
        )
        try:
            llm_result = self._llm.generate(messages, max_tokens=8192)
        except Exception as exc:
            raise WorkloadRewriteError(
                f"LLM request failed while rewriting query batch {batch_index or 1}: {exc}",
                request_payload=request_payload,
            ) from exc

        if llm_result.response_text.lstrip().startswith("{"):
            try:
                proposal = self._proposal_from_response(llm_result.response_text, target_tables)
                proposal.raw_response_text = llm_result.response_text
                proposal.request_payload = request_payload
                return proposal
            except ValueError:
                pass

        direct_sql_proposal = self._proposal_from_direct_sql(llm_result.response_text, target_tables)
        if direct_sql_proposal is not None:
            direct_sql_proposal.request_payload = request_payload
            return direct_sql_proposal

        raise WorkloadRewriteError(
            "LLM response did not contain executable rewritten SQL.",
            raw_response_text=llm_result.response_text,
            request_payload=request_payload,
        )

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
        response_text = self._extract_sql_text(response_text)
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
        source_tables: list[dict] | None = None,
    ) -> list[dict[str, str]]:
        system_prompt = (
            "/no_think\n"
            "You rewrite SQL workloads from one PostgreSQL schema to another. "
            "Output only executable SQL statements."
        )
        source_schema = self._schema_prompt(source_tables or [], include_schema=True)
        target_schema = self._target_schema_prompt(target_tables)
        user_prompt = (
            (
                f"Rewrite workload query {batch_index} so it targets the target schema instead of the source schema.\n\n"
                if batch_index is not None
                else "Rewrite these workload queries so they target the target schema instead of the source schema.\n\n"
            )
            + "Rules:\n"
            "0. Do not think step by step. Do not explain. Produce the final SQL directly.\n"
            "1. Output only SQL, with one rewritten statement for each source statement, in the same order.\n"
            "2. Do not output JSON, markdown, comments, explanations, EXPLAIN, CREATE, ALTER, DROP, TRUNCATE, GRANT, or REVOKE.\n"
            "3. Use target table and column names exactly as shown in the target schema.\n"
            "4. Preserve projections, filters, joins, ordering, limits, placeholders, random expressions, and literal types as closely as possible.\n"
            "5. WITH queries are allowed when they lead to SELECT/INSERT/UPDATE/DELETE.\n"
            "6. Use the source schema to understand the original table and column meanings before choosing target columns.\n\n"
            f"Source workload statements:\n{self._numbered_sql(source_statements)}\n\n"
            f"Source schema:\n{source_schema}\n\n"
            f"Target schema:\n{target_schema}"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _target_schema_prompt(self, target_tables: list[dict]) -> str:
        return self._schema_prompt(target_tables, include_schema=False)

    def _schema_prompt(self, tables: list[dict], *, include_schema: bool) -> str:
        blocks: list[str] = []
        for table in tables:
            table_name = str(table.get("name", "")).strip()
            if not table_name:
                continue
            schema = str(table.get("schema", "")).strip()
            qualified_name = f"{schema}.{table_name}" if include_schema and schema else table_name
            columns = [
                self._column_prompt(column)
                for column in table.get("columns", [])
                if str(column.get("name", "")).strip()
            ]
            column_sql = ", ".join(columns) if columns else "/* columns unknown */"
            blocks.append(f"CREATE TABLE {qualified_name} ({column_sql});")
        return "\n".join(blocks) if blocks else "/* schema unavailable */"

    def _column_prompt(self, column: dict) -> str:
        name = str(column.get("name", "")).strip()
        data_type = str(column.get("data_type", "") or column.get("type", "") or "").strip()
        return f'"{name}" {data_type}' if data_type else f'"{name}"'

    def _build_validation_repair_messages(
        self,
        source_statement: str,
        previous_rewrite: str,
        validation_error: dict,
        target_tables: list[dict],
        migration_statements: list[dict],
        *,
        query_index: int,
        source_tables: list[dict] | None = None,
    ) -> list[dict[str, str]]:
        source_schema = self._schema_prompt(source_tables or [], include_schema=True)
        target_schema = self._target_schema_prompt(target_tables)
        return [
            {
                "role": "system",
                "content": (
                    "/no_think\n"
                    "You repair PostgreSQL query rewrites. Output only the corrected executable SQL statement."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Repair rewritten query {query_index}. PostgreSQL rejected the previous rewrite.\n\n"
                    "Rules:\n"
                    "1. Output only one corrected SQL statement.\n"
                    "2. Do not output JSON, markdown, comments, explanations, EXPLAIN, CREATE, ALTER, DROP, TRUNCATE, GRANT, or REVOKE.\n"
                    "3. The corrected statement must start with SELECT, WITH, INSERT, UPDATE, or DELETE.\n"
                    "4. Use target table and column names exactly as shown in the target schema.\n"
                    "5. Preserve the source query semantics as closely as the target schema permits.\n\n"
                    f"Original source query:\n{source_statement.strip()}\n\n"
                    f"Previous rejected rewrite:\n{previous_rewrite.strip()}\n\n"
                    f"PostgreSQL validation error:\n{json.dumps(validation_error, ensure_ascii=True, indent=2)}\n\n"
                    f"Source schema:\n{source_schema}\n\n"
                    f"Target schema:\n{target_schema}\n\n"
                    f"Data migration statements, if useful for mapping columns:\n{json.dumps(migration_statements, ensure_ascii=True, indent=2)}"
                ),
            },
        ]

    def _numbered_sql(self, statements: list[str]) -> str:
        return "\n\n".join(
            f"-- query {index}\n{statement.strip()}"
            for index, statement in enumerate(statements, start=1)
        )

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
                    "You repair malformed query rewrite output. Return one JSON object only. "
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
        without_meta = PGBENCH_META_COMMAND_RE.sub("", without_line_comments)
        return MARKDOWN_FENCE_RE.sub("", without_meta)

    def _extract_sql_text(self, text: str) -> str:
        fence_match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
        if fence_match:
            return fence_match.group(1)
        first_sql = re.search(r"\b(WITH|SELECT|INSERT|UPDATE|DELETE)\b", text, re.IGNORECASE)
        return text[first_sql.start():] if first_sql else text

    def _summary_table_candidates(self, log_summary: LogSummary) -> list[dict]:
        candidates: list[dict] = []
        for index, query in enumerate(log_summary.top_queries, start=1):
            sql = query.sample_sql.strip()
            normalized = re.sub(r"\s+", " ", sql, flags=re.MULTILINE).strip()
            if not normalized:
                continue
            has_aggregate = bool(re.search(r"\b(count|sum|avg|min|max)\s*\(", normalized, re.IGNORECASE))
            group_by = re.search(r"\bgroup\s+by\s+(?P<columns>.+?)(?:\border\s+by\b|\blimit\b|$)", normalized, re.IGNORECASE)
            has_order_by = bool(re.search(r"\border\s+by\b", normalized, re.IGNORECASE))
            has_limit = bool(re.search(r"\blimit\s+\d+\b", normalized, re.IGNORECASE))
            has_join = bool(re.search(r"\bjoin\b", normalized, re.IGNORECASE))
            if not ((has_aggregate and group_by) or (has_order_by and has_limit and has_join)):
                continue
            projected_columns = self._projected_columns(normalized)
            pattern = "aggregate_group_by" if has_aggregate and group_by else "stable_top_n"
            reason = (
                "Repeated aggregate query with GROUP BY could be precomputed as a summary table."
                if pattern == "aggregate_group_by"
                else "Repeated ordered top-N query may remain stable long enough to precompute as a summary table."
            )
            group_columns = []
            if group_by:
                group_columns = [
                    item.strip().strip('"')
                    for item in group_by.group("columns").rstrip(";").split(",")
                    if item.strip()
                ][:6]
            table_name = f"summary_workload_{len(candidates) + 1}"
            candidates.append(
                {
                    "label": f"Summary candidate {len(candidates) + 1}",
                    "pattern": pattern,
                    "query_frequency": query.count,
                    "grouping_column_count": len(group_columns),
                    "grouping_columns_sample": group_columns,
                    "reason": reason,
                    "creation_status": "approval_required",
                    "table_name": table_name,
                    "sql_statement": self._summary_table_sql(table_name, projected_columns),
                    "sample_query": normalized[:500],
                }
            )
            if len(candidates) >= 8:
                break
        return candidates

    def _projected_columns(self, sql: str) -> list[str]:
        match = re.search(r"\bselect\s+(?P<select>.*?)\s+\bfrom\b", sql, re.IGNORECASE | re.DOTALL)
        if not match:
            return ["summary_value"]
        columns = []
        for index, item in enumerate(match.group("select").split(","), start=1):
            item = item.strip()
            alias = re.search(r"\bas\s+\"?(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\"?$", item, re.IGNORECASE)
            if alias:
                columns.append(alias.group("alias").lower())
                continue
            bare = item.split(".")[-1].strip().strip('"')
            bare = re.sub(r"[^A-Za-z0-9_]+", "_", bare).strip("_").lower()
            columns.append(bare or f"value_{index}")
        return columns[:12] or ["summary_value"]

    def _summary_table_sql(self, table_name: str, columns: list[str]) -> str:
        column_defs = ",\n  ".join(
            f'"{column}" text'
            for column in columns
        )
        return f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n  {column_defs}\n);'

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
