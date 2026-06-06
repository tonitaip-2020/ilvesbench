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
DEFAULT_MAX_WORKLOAD_WEIGHT = 100


@dataclass(slots=True)
class WorkloadPlan:
    status: str
    summary: str
    candidate_summary_tables: list[dict] = field(default_factory=list)
    source: str = "fallback"
    raw_response_text: str | None = None
    request_payload: dict | None = None


@dataclass(slots=True)
class WorkloadRewriteProposal:
    status: str
    summary: str
    rationale: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    source: str = "fallback"
    raw_response_text: str | None = None
    request_payload: dict | None = None


@dataclass(slots=True)
class PgBenchWorkloadPlan:
    status: str
    summary: str
    workload_sql: str = ""
    query_mix: list[dict] = field(default_factory=list)
    source: str = "workload_file"


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
                source="workload_heuristic",
            )
        return WorkloadPlan(
            status="no_recommendations",
            summary=(
                "Workload extraction is active. No obvious summary-table candidate was detected yet."
            ),
            source="workload_heuristic",
        )

    def recommend_summary_tables(
        self,
        log_summary: LogSummary | None,
        *,
        source_tables: list[dict] | None = None,
        target_tables: list[dict] | None = None,
        batch_size: int = 20,
    ) -> WorkloadPlan:
        baseline = self.build_plan(log_summary)
        if log_summary is None or not log_summary.top_queries:
            return baseline
        if self._llm is None:
            return baseline

        observations = [
            {
                "query_id": f"query_{index}",
                "count": query.count,
                "sample_sql": query.sample_sql,
                "fingerprint": query.fingerprint,
                "total_duration_ms": query.total_duration_ms,
            }
            for index, query in enumerate(log_summary.top_queries, start=1)
            if query.sample_sql.strip()
        ]
        if not observations:
            return WorkloadPlan(
                status="no_queries",
                summary="The workload source was parsed, but no executable SQL statements were detected.",
                source="llm",
            )

        chunks = [observations[index : index + batch_size] for index in range(0, len(observations), batch_size)]
        candidates: list[dict] = []
        raw_responses: list[str] = []
        request_batches: list[dict] = []
        for batch_index, chunk in enumerate(chunks, start=1):
            request_payload = {
                "mode": "summary_table_recommendations",
                "batch_index": batch_index,
                "batch_count": len(chunks),
                "queries": chunk,
                "source_tables": source_tables or [],
                "target_tables": target_tables or [],
                "previous_recommendations": candidates,
            }
            request_batches.append(request_payload)
            messages = self._build_summary_table_messages(request_payload)
            try:
                llm_result = self._llm.generate(messages, max_tokens=8192)
            except Exception as exc:
                if baseline.candidate_summary_tables:
                    return WorkloadPlan(
                        status=baseline.status,
                        summary=f"LLM summary-table recommendation failed, so heuristic candidates were used: {exc}",
                        candidate_summary_tables=baseline.candidate_summary_tables,
                        source="workload_heuristic_fallback",
                        request_payload={"batches": request_batches},
                    )
                raise
            raw_responses.append(f"Batch {batch_index}:\n{llm_result.response_text}")
            candidates.extend(
                self._summary_recommendations_from_response(
                    llm_result.response_text,
                    existing_candidates=candidates,
                )
            )

        if not candidates:
            return WorkloadPlan(
                status="no_recommendations",
                summary=(
                    "The LLM did not recommend summary tables. The workload does not currently show "
                    "a repeated, stable, expensive pattern that clearly warrants precomputed tables."
                ),
                source="llm",
                raw_response_text="\n\n".join(raw_responses),
                request_payload={"mode": "batched", "batch_count": len(chunks), "batches": request_batches},
            )
        return WorkloadPlan(
            status="recommended",
            summary=f"Recommended {len(candidates)} summary table candidate(s) for human review.",
            candidate_summary_tables=candidates,
            source="llm",
            raw_response_text="\n\n".join(raw_responses),
            request_payload={"mode": "batched", "batch_count": len(chunks), "batches": request_batches},
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

    def build_pgbench_workload(
        self,
        statements: list[str],
        *,
        source_kind: str,
        max_total_weight: int = DEFAULT_MAX_WORKLOAD_WEIGHT,
    ) -> PgBenchWorkloadPlan:
        if source_kind == "postgres_log":
            return PgBenchWorkloadPlan(
                status="placeholder",
                summary=(
                    "PostgreSQL log based workload inference is a placeholder. "
                    "Use a workload SQL file for pgbench-ready workload generation in this prototype."
                ),
                source=source_kind,
            )
        normalized_statements = [
            self._normalize_statement(statement, set())
            for statement in statements
            if str(statement).strip()
        ]
        if not normalized_statements:
            return PgBenchWorkloadPlan(
                status="no_queries",
                summary="No executable query statements were available for pgbench workload generation.",
                source=source_kind,
            )

        ordered: list[dict] = []
        by_key: dict[str, dict] = {}
        for statement in normalized_statements:
            key = self._workload_key(statement)
            if key not in by_key:
                item = {"statement": statement, "count": 0}
                by_key[key] = item
                ordered.append(item)
            by_key[key]["count"] += 1

        counts = [int(item["count"]) for item in ordered]
        weights = self._workload_weights(counts, max_total_weight=max_total_weight)
        total_count = sum(counts)
        query_mix: list[dict] = []
        sql_blocks = [
            "-- IlvesBench pgbench workload",
            f"-- source_kind: {source_kind}",
            "-- Literal constants from workload files are preserved. Only explicit question-mark placeholders are converted.",
        ]
        for index, (item, weight) in enumerate(zip(ordered, weights, strict=False), start=1):
            statement = str(item["statement"]).strip()
            count = int(item["count"])
            proportion = count / total_count if total_count else 0.0
            prepared_statement, placeholder_count = self._pgbench_placeholder_statement(statement, query_index=index)
            query_mix.append(
                {
                    "query_id": f"query_{index}",
                    "statement": statement,
                    "count": count,
                    "proportion": round(proportion, 6),
                    "proportion_percent": round(proportion * 100, 3),
                    "weight": weight,
                    "placeholder_count": placeholder_count,
                }
            )
            for repetition in range(weight):
                sql_blocks.append(
                    "\n".join(
                        [
                            (
                                f"-- query_{index} repeat {repetition + 1}/{weight} | "
                                f"proportion {proportion * 100:.3f}% | observed {count}"
                            ),
                            prepared_statement,
                        ]
                    )
                )
        return PgBenchWorkloadPlan(
            status="completed",
            summary=(
                f"Generated pgbench workload mix with {len(query_mix)} distinct query statement(s) "
                f"and {sum(weights)} weighted execution block(s)."
            ),
            workload_sql="\n\n".join(sql_blocks).strip() + "\n",
            query_mix=query_mix,
            source=source_kind,
        )

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

    def _workload_key(self, statement: str) -> str:
        return re.sub(r"\s+", " ", statement.strip().rstrip(";")).strip()

    def _workload_weights(self, counts: list[int], *, max_total_weight: int) -> list[int]:
        if not counts:
            return []
        divisor = counts[0]
        for count in counts[1:]:
            divisor = self._gcd(divisor, count)
        reduced = [max(1, count // max(divisor, 1)) for count in counts]
        if sum(reduced) <= max_total_weight:
            return reduced
        total = sum(counts)
        scaled = [max(1, round((count / total) * max_total_weight)) for count in counts]
        return scaled

    def _gcd(self, left: int, right: int) -> int:
        left, right = abs(left), abs(right)
        while right:
            left, right = right, left % right
        return left or 1

    def _pgbench_placeholder_statement(self, statement: str, *, query_index: int) -> tuple[str, int]:
        chars: list[str] = []
        placeholder_index = 0
        in_single = False
        in_double = False
        for char in statement:
            if char == "'" and not in_double:
                in_single = not in_single
            elif char == '"' and not in_single:
                in_double = not in_double
            if char == "?" and not in_single and not in_double:
                placeholder_index += 1
                chars.append(f":ilves_q{query_index}_p{placeholder_index}")
                continue
            chars.append(char)
        prepared = "".join(chars).strip()
        if not prepared.endswith(";"):
            prepared += ";"
        if placeholder_index == 0:
            return prepared, 0
        variables = [
            f"\\set ilves_q{query_index}_p{index} 1"
            for index in range(1, placeholder_index + 1)
        ]
        return "\n".join(variables + [prepared]), placeholder_index

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
                    "id": f"summary_{len(candidates) + 1}",
                    "label": f"Summary candidate {len(candidates) + 1}",
                    "status": "pending",
                    "pattern": pattern,
                    "query_ids": [f"query_{index}"],
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

    def _build_summary_table_messages(self, payload: dict) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "/no_think\n"
                    "You recommend PostgreSQL summary tables for workload acceleration. Return JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Recommend summary tables for the target PostgreSQL database.\n\n"
                    "Rules:\n"
                    "1. Recommend summary tables only when they are clearly justified by repeated, expensive, stable query patterns.\n"
                    "2. Prefer no recommendation when ordinary indexes, fresh base-table reads, or query rewrites are likely enough.\n"
                    "3. Good candidates include repeated aggregates, stable top-N reports, recurring multi-table reporting joins, and low-staleness analytical snapshots.\n"
                    "4. Avoid recommending a summary table for point lookups, highly selective transactional queries, volatile data, or one-off queries.\n"
                    "5. SQL must contain only CREATE TABLE or CREATE TABLE IF NOT EXISTS statements. Do not include INSERT, SELECT AS, materialized views, indexes, triggers, comments, or multiple statements.\n"
                    "6. Use target table and column names when the target schema is available. Summary tables will be created before query rewrites and before index recommendations.\n"
                    "7. previous_recommendations contains accepted recommendations from earlier batches; avoid duplicates and redundant summary tables.\n\n"
                    "Return JSON with this shape:\n"
                    "{\n"
                    '  "status": "recommended|no_recommendations",\n'
                    '  "summary": "short summary",\n'
                    '  "recommendations": [\n'
                    "    {\n"
                    '      "label": "short label",\n'
                    '      "table_name": "summary_table_name",\n'
                    '      "pattern": "aggregate_group_by|stable_top_n|reporting_join|other",\n'
                    '      "query_ids": ["query_1"],\n'
                    '      "reason": "why this is truly needed",\n'
                    '      "freshness_expectation": "why staleness is acceptable",\n'
                    '      "confidence": 0.0,\n'
                    '      "sql_statement": "CREATE TABLE IF NOT EXISTS ...;"\n'
                    "    }\n"
                    "  ]\n"
                    "}\n\n"
                    f"Payload:\n{json.dumps(payload, ensure_ascii=True, indent=2)}"
                ),
            },
        ]

    def _summary_recommendations_from_response(
        self,
        response_text: str,
        *,
        existing_candidates: list[dict],
    ) -> list[dict]:
        payload = self._extract_json_object(response_text)
        candidates: list[dict] = []
        used_names = {
            str(candidate.get("table_name", "")).strip().lower()
            for candidate in existing_candidates
            if str(candidate.get("table_name", "")).strip()
        }
        next_index = len(existing_candidates) + 1
        for item in payload.get("recommendations", []):
            if not isinstance(item, dict):
                continue
            sql_statement = self._normalize_summary_table_sql(str(item.get("sql_statement", "")))
            if not sql_statement:
                continue
            table_name = self._summary_table_name(str(item.get("table_name", "")), sql_statement)
            if not table_name:
                continue
            dedupe_name = table_name.lower()
            if dedupe_name in used_names:
                continue
            used_names.add(dedupe_name)
            candidates.append(
                {
                    "id": f"summary_{next_index}",
                    "label": str(item.get("label") or f"Summary candidate {next_index}"),
                    "status": "pending",
                    "pattern": str(item.get("pattern", "other") or "other"),
                    "query_ids": [str(value) for value in item.get("query_ids", []) if str(value).strip()],
                    "reason": str(item.get("reason", "")),
                    "freshness_expectation": str(item.get("freshness_expectation", "")),
                    "confidence": self._confidence(item.get("confidence", 0.5)),
                    "creation_status": "approval_required",
                    "table_name": table_name,
                    "sql_statement": sql_statement,
                }
            )
            next_index += 1
        return candidates

    def _normalize_summary_table_sql(self, sql_text: str) -> str:
        statement = sql_text.strip().rstrip(";")
        if not statement:
            return ""
        if ";" in statement:
            return ""
        if not re.match(r"^\s*create\s+table(?:\s+if\s+not\s+exists)?\b", statement, re.IGNORECASE):
            return ""
        if re.search(r"\b(insert|update|delete|drop|alter|truncate|create\s+index|create\s+materialized\s+view)\b", statement, re.IGNORECASE):
            return ""
        return statement + ";"

    def _summary_table_name(self, supplied_name: str, sql_statement: str) -> str:
        name = re.sub(r"[^A-Za-z0-9_]+", "_", supplied_name.strip().strip('"')).strip("_").lower()
        if name:
            return name
        match = re.search(
            r"create\s+table(?:\s+if\s+not\s+exists)?\s+(?:\"(?P<quoted>[A-Za-z_][A-Za-z0-9_]*)\"|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))",
            sql_statement,
            re.IGNORECASE,
        )
        if not match:
            return ""
        return (match.group("quoted") or match.group("plain") or "").lower()

    def _confidence(self, value) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, confidence))

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
