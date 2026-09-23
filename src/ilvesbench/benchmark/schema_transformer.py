from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict, deque
import math
import json
import re

from ilvesbench.config import SchemaChunkingConfig
from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import SchemaSnapshot, TableMetadata


@dataclass(slots=True)
class NormalizationProposal:
    status: str
    summary: str
    target_database: str | None = None
    rationale: list[str] = field(default_factory=list)
    table_findings: list[str] = field(default_factory=list)
    functional_dependencies: list[dict] = field(default_factory=list)
    target_tables: list[dict] = field(default_factory=list)
    sql_statements: list[str] = field(default_factory=list)
    source: str = "metadata_fallback"
    raw_response_text: str | None = None
    request_payload: dict | None = None


class SchemaTransformer:
    """LLM-assisted normalization proposal with deterministic validation and SQL generation."""

    def __init__(
        self,
        llm: LLMGateway | None = None,
        target_database: str | None = None,
        chunking: SchemaChunkingConfig | None = None,
    ) -> None:
        self._llm = llm
        self._target_database = target_database
        self._chunking = chunking or SchemaChunkingConfig()

    def analyze(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict] | None = None,
    ) -> NormalizationProposal:
        first_normal_form_findings = first_normal_form_findings or []
        if not schema.tables:
            return NormalizationProposal(
                status="no_tables",
                summary="No user tables were found in the inspected schemas.",
            )

        if self._llm is not None:
            try:
                request_payload = self._request_payload(schema, first_normal_form_findings)
                full_messages = self._build_messages(schema, first_normal_form_findings)
                if self._chunking.enabled and not self._messages_fit(full_messages):
                    return self._analyze_in_chunks(schema, first_normal_form_findings)
                llm_result = self._llm.generate(
                    full_messages,
                    max_tokens=self._chunking.reserved_output_tokens,
                )
                proposal = self._proposal_from_llm_response(schema, llm_result.response_text)
                proposal.raw_response_text = llm_result.response_text
                proposal.request_payload = request_payload
                if first_normal_form_findings and not proposal.target_tables:
                    fallback = self._first_normal_form_decomposition(schema, first_normal_form_findings)
                    if fallback is not None:
                        fallback.raw_response_text = llm_result.response_text
                        fallback.request_payload = request_payload
                        fallback.rationale.insert(0, "LLM returned no target tables despite deterministic 1NF warnings.")
                        fallback.source = "deterministic_1nf_fallback_after_llm"
                        return fallback
                return proposal
            except Exception as exc:
                fallback = self._metadata_fallback(schema, first_normal_form_findings)
                fallback.summary = (
                    "LLM normalization proposal could not be validated, so IlvesBench fell back to metadata-only analysis."
                )
                fallback.request_payload = self._request_payload(schema, first_normal_form_findings)
                fallback.rationale.insert(0, f"LLM proposal fallback reason: {exc}")
                return fallback

        return self._metadata_fallback(schema, first_normal_form_findings)

    def repair(
        self,
        schema: SchemaSnapshot,
        existing_target_tables: list[dict],
        existing_sql_statements: list[str],
        error_message: str,
    ) -> NormalizationProposal:
        if self._llm is None:
            raise ValueError("Schema repair requires an LLM gateway.")

        llm_result = self._llm.generate(
            self._build_repair_messages(schema, existing_target_tables, existing_sql_statements, error_message),
            max_tokens=1800,
        )
        payload = self._extract_json_object(llm_result.response_text)
        target_tables = self._normalize_target_tables(schema, payload.get("target_tables", []))
        sql_statements = self._generate_sql(target_tables, schema)
        return NormalizationProposal(
            status=str(payload.get("status", "candidate_normalization")),
            summary=str(payload.get("summary", "Schema SQL was repaired after a PostgreSQL error.")),
            target_database=self._target_database,
            rationale=self._to_string_list(payload.get("reasoning", [])),
            table_findings=self._to_string_list(payload.get("table_findings", [])),
            functional_dependencies=self._normalize_functional_dependencies(payload.get("functional_dependencies", [])),
            target_tables=target_tables,
            sql_statements=sql_statements,
            source="llm_repair",
            raw_response_text=llm_result.response_text,
            request_payload={
                "source_schema": self._schema_summary(schema),
                "existing_target_tables": existing_target_tables,
                "existing_sql_statements": existing_sql_statements,
                "error_message": error_message,
            },
        )

    def build_first_normal_form_decomposition(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict],
    ) -> NormalizationProposal | None:
        return self._first_normal_form_decomposition(schema, first_normal_form_findings)

    def discover_functional_dependencies(self, schema: SchemaSnapshot, target_tables: list[dict]) -> NormalizationProposal:
        if not target_tables:
            return NormalizationProposal(
                status="no_tables",
                summary="No 1NF target tables were available for 3NF functional-dependency discovery.",
                source="fd_discovery_skipped",
            )
        if self._llm is None:
            return NormalizationProposal(
                status="insufficient_evidence",
                summary="Functional-dependency discovery requires an LLM gateway for semantic analysis.",
                target_tables=target_tables,
                source="fd_discovery_unavailable",
                request_payload=self._fd_request_payload(schema, target_tables),
            )

        request_payload = self._fd_request_payload(schema, target_tables)
        try:
            full_messages = self._build_fd_discovery_messages(schema, target_tables)
            if self._chunking.enabled and not self._messages_fit(full_messages):
                return self._discover_functional_dependencies_in_chunks(schema, target_tables)
            llm_result = self._llm.generate(
                full_messages,
                max_tokens=self._chunking.reserved_output_tokens,
            )
            payload = self._extract_json_object(llm_result.response_text)
            functional_dependencies = self._normalize_functional_dependencies(payload.get("functional_dependencies", []))
            return NormalizationProposal(
                status=str(payload.get("status", "candidate_normalization")),
                summary=str(payload.get("summary", f"Found {len(functional_dependencies)} candidate functional dependenc(ies).")),
                target_database=self._target_database,
                rationale=self._to_string_list(payload.get("reasoning", [])),
                table_findings=self._to_string_list(payload.get("table_findings", [])),
                functional_dependencies=functional_dependencies,
                target_tables=target_tables,
                source="llm_fd_discovery",
                raw_response_text=llm_result.response_text,
                request_payload=request_payload,
            )
        except Exception as exc:
            return NormalizationProposal(
                status="insufficient_evidence",
                summary="Functional-dependency discovery failed; IlvesBench will continue with the approved 1NF decomposition.",
                target_tables=target_tables,
                rationale=[f"FD discovery failure: {exc}"],
                source="fd_discovery_failed",
                request_payload=request_payload,
            )

    def synthesize_third_normal_form(
        self,
        schema: SchemaSnapshot,
        target_tables: list[dict],
        approved_functional_dependencies: list[dict],
    ) -> NormalizationProposal:
        synthesized_tables = self._synthesize_3nf_tables(target_tables, approved_functional_dependencies)
        sql_statements = self._generate_sql(synthesized_tables, schema)
        return NormalizationProposal(
            status="candidate_normalization",
            summary=(
                f"Generated deterministic 3NF target schema with {len(synthesized_tables)} table(s) "
                f"from {len(approved_functional_dependencies)} approved functional dependenc(ies)."
            ),
            target_database=self._target_database,
            rationale=[
                "Used approved candidate functional dependencies as input to a deterministic 3NF synthesis pass.",
                "Each non-key determinant creates a relation containing determinant and dependent columns; dependent columns are removed from the original relation when safe.",
            ],
            table_findings=[
                str(item.get("reason", ""))
                for item in approved_functional_dependencies
                if str(item.get("reason", "")).strip()
            ],
            functional_dependencies=approved_functional_dependencies,
            target_tables=synthesized_tables,
            sql_statements=sql_statements,
            source="deterministic_3nf_synthesis",
        )

    def _proposal_from_llm_response(self, schema: SchemaSnapshot, response_text: str) -> NormalizationProposal:
        payload = self._extract_json_object(response_text)
        target_tables = self._normalize_target_tables(schema, payload.get("target_tables", []))
        functional_dependencies = self._normalize_functional_dependencies(payload.get("functional_dependencies", []))
        sql_statements = self._generate_sql(target_tables, schema)

        assessment = payload.get("assessment", {})
        summary = payload.get("decomposition_summary") or assessment.get("summary") or "Normalization proposal created."
        rationale = assessment.get("reasoning", [])
        findings = payload.get("table_findings", [])
        proposed_table_count = len(target_tables) if target_tables else len(schema.tables)

        if not target_tables:
            status = assessment.get("status") or "appears_3nf"
        else:
            status = assessment.get("status") or "candidate_normalization"

        return NormalizationProposal(
            status=status,
            summary=summary,
            target_database=self._target_database,
            rationale=self._to_string_list(rationale),
            table_findings=self._to_string_list(findings),
            functional_dependencies=functional_dependencies,
            target_tables=target_tables,
            sql_statements=sql_statements,
            source="llm",
        )

    def _request_payload(self, schema: SchemaSnapshot, first_normal_form_findings: list[dict]) -> dict:
        return {
            "source_schema": self._schema_summary(schema),
            "first_normal_form_findings": first_normal_form_findings,
            "target_database": self._target_database,
        }

    def _fd_request_payload(self, schema: SchemaSnapshot, target_tables: list[dict]) -> dict:
        return {
            "source_schema": self._schema_summary(schema),
            "one_nf_target_tables": target_tables,
            "target_database": self._target_database,
        }

    def _prompt_budget_tokens(self) -> int:
        return max(
            1,
            self._chunking.context_window_tokens
            - self._chunking.reserved_output_tokens
            - self._chunking.safety_margin_tokens,
        )

    def _estimate_message_tokens(self, messages: list[dict[str, str]]) -> int:
        characters = sum(len(message.get("role", "")) + len(message.get("content", "")) for message in messages)
        characters_per_token = max(1.0, float(self._chunking.estimated_characters_per_token))
        return math.ceil(characters / characters_per_token)

    def _messages_fit(self, messages: list[dict[str, str]]) -> bool:
        return self._estimate_message_tokens(messages) <= self._prompt_budget_tokens()

    def _subset_schema(self, schema: SchemaSnapshot, tables: list[TableMetadata]) -> SchemaSnapshot:
        return SchemaSnapshot(
            database=schema.database,
            collected_at=schema.collected_at,
            tables=tables,
            database_size_bytes=schema.database_size_bytes,
        )

    def _schema_relationship_catalog(self, schema: SchemaSnapshot) -> dict:
        possible_associations = self._possible_implicit_associations(schema)
        return {
            "database": schema.database,
            "tables": [
                {
                    "table": f"{table.schema}.{table.name}",
                    "column_count": len(table.columns),
                    "unique_constraints": [constraint.columns for constraint in table.unique_constraints],
                    "foreign_keys": [
                        {
                            "columns": foreign_key.columns,
                            "referenced_table": foreign_key.referenced_table,
                            "referenced_columns": foreign_key.referenced_columns,
                        }
                        for foreign_key in table.foreign_keys
                    ],
                }
                for table in schema.tables
            ],
            "possible_associations": possible_associations,
            "association_warning": (
                "Possible associations are name/type matches only. They are not declared constraints or proof of a relationship."
            ),
        }

    def _possible_implicit_associations(self, schema: SchemaSnapshot) -> list[dict]:
        occurrences: dict[tuple[str, str], list[str]] = defaultdict(list)
        for table in schema.tables:
            table_name = f"{table.schema}.{table.name}"
            for column in table.columns:
                key = (column.name.casefold(), self._canonical_data_type(column.data_type))
                occurrences[key].append(table_name)

        associations: list[dict] = []
        for (column_name, data_type), table_names in sorted(occurrences.items()):
            distinct_tables = sorted(set(table_names))
            if len(distinct_tables) < 2 or len(distinct_tables) > 8:
                continue
            if column_name in {"id", "name", "date", "type", "status", "value", "description"}:
                continue
            for index, left in enumerate(distinct_tables):
                for right in distinct_tables[index + 1 :]:
                    associations.append(
                        {
                            "left": f"{left}.{column_name}",
                            "right": f"{right}.{column_name}",
                            "data_type": data_type,
                            "status": "unverified",
                        }
                    )
                    if len(associations) >= 200:
                        return associations
        return associations

    def _relationship_order(self, schema: SchemaSnapshot, catalog: dict) -> list[TableMetadata]:
        tables_by_name = {f"{table.schema}.{table.name}": table for table in schema.tables}
        adjacency: dict[str, set[str]] = {name: set() for name in tables_by_name}
        for table_name, table in tables_by_name.items():
            for foreign_key in table.foreign_keys:
                referenced = foreign_key.referenced_table
                if referenced in adjacency:
                    adjacency[table_name].add(referenced)
                    adjacency[referenced].add(table_name)
        for association in catalog.get("possible_associations", []):
            left = str(association.get("left", "")).rsplit(".", 1)[0]
            right = str(association.get("right", "")).rsplit(".", 1)[0]
            if left in adjacency and right in adjacency:
                adjacency[left].add(right)
                adjacency[right].add(left)

        ordered: list[TableMetadata] = []
        visited: set[str] = set()
        for start in sorted(tables_by_name):
            if start in visited:
                continue
            queue = deque([start])
            visited.add(start)
            while queue:
                current = queue.popleft()
                ordered.append(tables_by_name[current])
                for neighbor in sorted(adjacency[current]):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
        return ordered

    def _normalization_chunks(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict],
    ) -> tuple[list[SchemaSnapshot], list[str], dict]:
        catalog = self._schema_relationship_catalog(schema)
        chunks: list[SchemaSnapshot] = []
        blocked: list[str] = []
        current: list[TableMetadata] = []
        for table in self._relationship_order(schema, catalog):
            table_name = f"{table.schema}.{table.name}"
            atomic_schema = self._subset_schema(schema, [table])
            atomic_findings = [item for item in first_normal_form_findings if item.get("table") == table_name]
            atomic_messages = self._build_messages(
                atomic_schema,
                atomic_findings,
                global_catalog=catalog,
                chunk_scope=[table_name],
            )
            if not self._messages_fit(atomic_messages):
                if current:
                    chunks.append(self._subset_schema(schema, current))
                    current = []
                blocked.append(table_name)
                continue

            candidate = current + [table]
            candidate_schema = self._subset_schema(schema, candidate)
            candidate_names = {f"{item.schema}.{item.name}" for item in candidate}
            candidate_findings = [
                item for item in first_normal_form_findings if item.get("table") in candidate_names
            ]
            candidate_messages = self._build_messages(
                candidate_schema,
                candidate_findings,
                global_catalog=catalog,
                chunk_scope=sorted(candidate_names),
            )
            if current and not self._messages_fit(candidate_messages):
                chunks.append(self._subset_schema(schema, current))
                current = [table]
            else:
                current = candidate
        if current:
            chunks.append(self._subset_schema(schema, current))
        return chunks, blocked, catalog

    def _analyze_in_chunks(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict],
    ) -> NormalizationProposal:
        chunks, blocked_tables, catalog = self._normalization_chunks(schema, first_normal_form_findings)
        proposals: list[NormalizationProposal] = []
        chunk_records: list[dict] = []
        failures: list[str] = []
        raw_responses: list[dict] = []

        for index, chunk in enumerate(chunks, start=1):
            table_names = [f"{table.schema}.{table.name}" for table in chunk.tables]
            findings = [item for item in first_normal_form_findings if item.get("table") in set(table_names)]
            messages = self._build_messages(
                chunk,
                findings,
                global_catalog=catalog,
                chunk_scope=table_names,
            )
            record = {
                "chunk": index,
                "tables": table_names,
                "estimated_input_tokens": self._estimate_message_tokens(messages),
            }
            try:
                result = self._llm.generate(messages, max_tokens=self._chunking.reserved_output_tokens)
                proposal = self._proposal_from_llm_response(schema, result.response_text)
                self._validate_normalization_chunk_scope(proposal, set(table_names))
                proposals.append(proposal)
                record["status"] = "completed"
                raw_responses.append({"chunk": index, "tables": table_names, "response_text": result.response_text})
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
                failures.append(f"Chunk {index} ({', '.join(table_names)}): {exc}")
            chunk_records.append(record)

        functional_dependencies = self._deduplicate_functional_dependencies(
            [fd for proposal in proposals for fd in proposal.functional_dependencies]
        )
        table_findings = list(dict.fromkeys(finding for proposal in proposals for finding in proposal.table_findings))
        rationale = list(dict.fromkeys(reason for proposal in proposals for reason in proposal.rationale))
        target_tables = self._merge_chunk_target_tables(proposals)
        incomplete_tables = blocked_tables or failures
        if blocked_tables:
            table_findings.extend(
                f"{table_name} is too large to analyze atomically within the configured prompt budget."
                for table_name in blocked_tables
            )
        if incomplete_tables:
            target_tables = []
            sql_statements: list[str] = []
            status = "requires_human_schema_partitioning" if blocked_tables else "insufficient_evidence"
            summary = (
                f"Chunked analysis completed for {len(chunks) - len(failures)} chunk(s), but automatic normalization "
                f"stopped because {len(blocked_tables)} table(s) exceeded the atomic-table prompt boundary."
                if blocked_tables
                else f"Chunked analysis was incomplete because {len(failures)} chunk(s) failed."
            )
        else:
            if target_tables:
                target_tables = self._ensure_complete_target_schema(schema, target_tables)
            sql_statements = self._generate_sql(target_tables, schema)
            status = "candidate_normalization" if target_tables else self._combined_noop_status(proposals)
            summary = (
                f"Analyzed {len(schema.tables)} table(s) in {len(chunks)} relationship-aware schema chunk(s); "
                f"produced {len(target_tables)} target table(s)."
            )

        return NormalizationProposal(
            status=status,
            summary=summary,
            target_database=self._target_database,
            rationale=[
                "The full schema exceeded the configured prompt budget, so IlvesBench used table-atomic chunks.",
                "Every chunk included a compact global relationship catalog; unverified name/type matches were context only.",
                *rationale,
                *failures,
            ],
            table_findings=table_findings,
            functional_dependencies=functional_dependencies,
            target_tables=target_tables,
            sql_statements=sql_statements,
            source="schema_chunking_boundary" if blocked_tables else "llm_chunked",
            raw_response_text=json.dumps(raw_responses, ensure_ascii=True),
            request_payload={
                "source_schema": self._schema_summary(schema),
                "first_normal_form_findings": first_normal_form_findings,
                "target_database": self._target_database,
                "chunking": self._chunking_manifest(chunk_records, blocked_tables),
            },
        )

    def _discover_functional_dependencies_in_chunks(
        self,
        schema: SchemaSnapshot,
        target_tables: list[dict],
    ) -> NormalizationProposal:
        source_catalog = self._schema_relationship_catalog(schema)
        target_catalog = [
            {
                "table": table.get("name", ""),
                "column_count": len(table.get("columns", [])),
                "primary_key": table.get("primary_key", []),
                "uniques": table.get("uniques", []),
                "foreign_keys": table.get("foreign_keys", []),
            }
            for table in target_tables
        ]
        dependencies: list[dict] = []
        findings: list[str] = []
        rationale: list[str] = []
        blocked_tables: list[str] = []
        failures: list[str] = []
        chunk_records: list[dict] = []
        raw_responses: list[dict] = []

        for index, table in enumerate(target_tables, start=1):
            table_name = str(table.get("name", ""))
            messages = self._build_fd_discovery_messages(
                schema,
                [table],
                source_catalog=source_catalog,
                target_catalog=target_catalog,
                chunk_scope=[table_name],
            )
            record = {
                "chunk": index,
                "tables": [table_name],
                "estimated_input_tokens": self._estimate_message_tokens(messages),
            }
            if not self._messages_fit(messages):
                blocked_tables.append(table_name)
                record["status"] = "requires_human_schema_partitioning"
                chunk_records.append(record)
                continue
            try:
                result = self._llm.generate(messages, max_tokens=self._chunking.reserved_output_tokens)
                payload = self._extract_json_object(result.response_text)
                chunk_dependencies = self._normalize_functional_dependencies(payload.get("functional_dependencies", []))
                self._validate_fd_chunk_scope(table, chunk_dependencies)
                dependencies.extend(chunk_dependencies)
                findings.extend(self._to_string_list(payload.get("table_findings", [])))
                rationale.extend(self._to_string_list(payload.get("reasoning", [])))
                record["status"] = "completed"
                raw_responses.append({"chunk": index, "tables": [table_name], "response_text": result.response_text})
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
                failures.append(f"FD chunk for {table_name}: {exc}")
            chunk_records.append(record)

        dependencies = self._deduplicate_functional_dependencies(dependencies)
        if blocked_tables:
            status = "requires_human_schema_partitioning"
            summary = (
                f"FD discovery skipped {len(blocked_tables)} table(s) because each table must fit atomically in one prompt."
            )
        elif failures:
            status = "insufficient_evidence"
            summary = f"FD discovery was incomplete because {len(failures)} table-scoped request(s) failed."
        else:
            status = "candidate_normalization" if dependencies else "appears_3nf"
            summary = f"Found {len(dependencies)} candidate functional dependenc(ies) in table-scoped chunks."
        return NormalizationProposal(
            status=status,
            summary=summary,
            target_database=self._target_database,
            rationale=[
                "The combined FD request exceeded the prompt budget, so each target table was analyzed atomically.",
                *list(dict.fromkeys(rationale)),
                *failures,
            ],
            table_findings=list(dict.fromkeys(findings)) + [
                f"{table_name} requires human schema partitioning before LLM-based FD discovery."
                for table_name in blocked_tables
            ],
            functional_dependencies=dependencies,
            target_tables=target_tables,
            source="fd_schema_chunking_boundary" if blocked_tables else "llm_fd_discovery_chunked",
            raw_response_text=json.dumps(raw_responses, ensure_ascii=True),
            request_payload={
                "source_schema": self._schema_summary(schema),
                "one_nf_target_tables": target_tables,
                "target_database": self._target_database,
                "chunking": self._chunking_manifest(chunk_records, blocked_tables),
            },
        )

    def _chunking_manifest(self, chunks: list[dict], blocked_tables: list[str]) -> dict:
        return {
            "enabled": self._chunking.enabled,
            "context_window_tokens": self._chunking.context_window_tokens,
            "reserved_output_tokens": self._chunking.reserved_output_tokens,
            "safety_margin_tokens": self._chunking.safety_margin_tokens,
            "estimated_characters_per_token": self._chunking.estimated_characters_per_token,
            "prompt_budget_tokens": self._prompt_budget_tokens(),
            "chunks": chunks,
            "blocked_atomic_tables": blocked_tables,
        }

    def _deduplicate_functional_dependencies(self, dependencies: list[dict]) -> list[dict]:
        result: list[dict] = []
        seen: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
        for dependency in dependencies:
            key = (
                str(dependency.get("table", "")),
                tuple(str(item) for item in dependency.get("determinant", [])),
                tuple(str(item) for item in dependency.get("dependent", [])),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(dependency)
        return result

    def _validate_normalization_chunk_scope(
        self,
        proposal: NormalizationProposal,
        allowed_source_tables: set[str],
    ) -> None:
        for dependency in proposal.functional_dependencies:
            table_name = str(dependency.get("table", ""))
            if table_name and table_name not in allowed_source_tables:
                raise ValueError(
                    f"Schema chunk returned a functional dependency for catalog-only table {table_name}."
                )
        for table in proposal.target_tables:
            proposed_sources = {str(item) for item in table.get("source_tables", [])}
            column_sources = {str(column.get("source_table", "")) for column in table.get("columns", [])}
            outside_scope = (proposed_sources | column_sources) - allowed_source_tables
            if outside_scope:
                raise ValueError(
                    f"Schema chunk target table {table.get('name', '')} uses catalog-only source table(s): "
                    f"{', '.join(sorted(outside_scope))}."
                )

    def _validate_fd_chunk_scope(self, table: dict, dependencies: list[dict]) -> None:
        table_name = str(table.get("name", ""))
        valid_columns = {
            str(column.get("name", ""))
            for column in table.get("columns", [])
            if str(column.get("name", ""))
        }
        for dependency in dependencies:
            if str(dependency.get("table", "")) != table_name:
                raise ValueError(
                    f"FD chunk for {table_name} returned a dependency for another table: "
                    f"{dependency.get('table', '')}."
                )
            referenced_columns = {
                str(column)
                for column in dependency.get("determinant", []) + dependency.get("dependent", [])
            }
            unknown_columns = referenced_columns - valid_columns
            if unknown_columns:
                raise ValueError(
                    f"FD chunk for {table_name} returned unknown column(s): {', '.join(sorted(unknown_columns))}."
                )

    def _merge_chunk_target_tables(self, proposals: list[NormalizationProposal]) -> list[dict]:
        merged: dict[str, dict] = {}
        for proposal in proposals:
            for table in proposal.target_tables:
                name = str(table.get("name", ""))
                if name in merged and merged[name] != table:
                    raise ValueError(f"Schema chunks proposed conflicting definitions for target table {name}.")
                merged[name] = table
        return list(merged.values())

    def _combined_noop_status(self, proposals: list[NormalizationProposal]) -> str:
        statuses = {proposal.status for proposal in proposals}
        if statuses and statuses.issubset({"appears_3nf"}):
            return "appears_3nf"
        return "insufficient_evidence"

    def _ensure_complete_target_schema(self, schema: SchemaSnapshot, target_tables: list[dict]) -> list[dict]:
        covered_sources = {
            str(source_table)
            for table in target_tables
            for source_table in table.get("source_tables", [])
        }
        missing = [
            table
            for table in schema.tables
            if f"{table.schema}.{table.name}" not in covered_sources
        ]
        if not missing:
            return target_tables
        name_map = self._target_table_name_map(schema.tables)
        used_names = {str(table.get("name", "")) for table in target_tables}
        result = [self._copy_target_table(table) for table in target_tables]
        copied_by_source: dict[str, dict] = {}
        for table in missing:
            source_name = f"{table.schema}.{table.name}"
            desired_name = name_map[source_name]
            target_name = desired_name if desired_name not in used_names else self._unique_target_name(desired_name, used_names)
            used_names.add(target_name)
            primary_key = self._first_unique_constraint(
                table,
                {self._sanitize_identifier(column.name) for column in table.columns},
            )
            copied = {
                "name": target_name,
                "purpose": f"Unchanged table copied from {source_name} after chunked analysis.",
                "source_tables": [source_name],
                "columns": [
                    {
                        "source_table": source_name,
                        "source_column": column.name,
                        "name": self._sanitize_identifier(column.name),
                    }
                    for column in table.columns
                ],
                "primary_key": primary_key,
                "uniques": [
                    self._sanitize_identifier_list(constraint.columns)
                    for constraint in table.unique_constraints
                    if self._sanitize_identifier_list(constraint.columns) != primary_key
                ],
                "foreign_keys": [],
                "migration_strategy": "copy_distinct",
            }
            result.append(copied)
            copied_by_source[source_name] = copied

        source_tables = {f"{table.schema}.{table.name}": table for table in schema.tables}
        for source_name, copied in copied_by_source.items():
            source_table = source_tables[source_name]
            local_names = {
                str(column.get("source_column", "")): str(column.get("name", ""))
                for column in copied["columns"]
            }
            for foreign_key in source_table.foreign_keys:
                referenced = self._target_for_source_key(
                    result,
                    foreign_key.referenced_table,
                    foreign_key.referenced_columns,
                )
                if referenced is None:
                    raise ValueError(
                        f"Chunked target schema cannot preserve foreign key {foreign_key.name} from {source_name}."
                    )
                referenced_table, referenced_columns = referenced
                copied["foreign_keys"].append(
                    {
                        "columns": [local_names[column] for column in foreign_key.columns],
                        "references_table": referenced_table,
                        "references_columns": referenced_columns,
                    }
                )
        return result

    def _target_for_source_key(
        self,
        target_tables: list[dict],
        source_table: str,
        source_columns: list[str],
    ) -> tuple[str, list[str]] | None:
        for target in target_tables:
            mapped_columns = {
                str(column.get("source_column", "")): str(column.get("name", ""))
                for column in target.get("columns", [])
                if str(column.get("source_table", "")) == source_table
            }
            if not all(column in mapped_columns for column in source_columns):
                continue
            target_columns = [mapped_columns[column] for column in source_columns]
            if target_columns == target.get("primary_key") or target_columns in target.get("uniques", []):
                return str(target.get("name", "")), target_columns
        return None

    def _schema_summary(self, schema: SchemaSnapshot) -> list[dict]:
        return [
            {
                "table": f"{table.schema}.{table.name}",
                "columns": [column.name for column in table.columns],
                "unique_constraints": [constraint.columns for constraint in table.unique_constraints],
                "foreign_keys": [
                    {
                        "columns": foreign_key.columns,
                        "references_table": foreign_key.referenced_table,
                        "references_columns": foreign_key.referenced_columns,
                    }
                    for foreign_key in table.foreign_keys
                ],
            }
            for table in schema.tables
        ]

    def _metadata_fallback(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict] | None = None,
    ) -> NormalizationProposal:
        return self._metadata_only_pass(schema, first_normal_form_findings or [])

    def _metadata_only_pass(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict],
    ) -> NormalizationProposal:
        """Metadata-only first pass. This is intentionally conservative."""

        findings: list[str] = []
        missing_keys: list[str] = []
        descriptor_overlap: list[str] = []
        repeating_groups: list[str] = []

        for table in schema.tables:
            key_columns = {column for constraint in table.unique_constraints for column in constraint.columns}
            if not key_columns:
                missing_keys.append(f"{table.schema}.{table.name} has no primary-key or unique constraint evidence.")

            numbered_groups = self._find_numbered_groups([column.name for column in table.columns])
            if numbered_groups:
                groups = ", ".join(sorted(numbered_groups))
                repeating_groups.append(
                    f"{table.schema}.{table.name} has repeated numbered columns ({groups}), which often indicate repeating groups."
                )

            for foreign_key in table.foreign_keys:
                for fk_column in foreign_key.columns:
                    if not fk_column.endswith("_id"):
                        continue
                    prefix = fk_column[: -len("_id")]
                    related_descriptors = [
                        column.name
                        for column in table.columns
                        if column.name in {f"{prefix}_name", f"{prefix}_code", f"{prefix}_type", prefix}
                    ]
                    if related_descriptors:
                        descriptor_overlap.append(
                            f"{table.schema}.{table.name} stores {', '.join(related_descriptors)} alongside {fk_column}, "
                            "which may indicate duplicated descriptive attributes."
                        )

        findings.extend(repeating_groups)
        findings.extend(descriptor_overlap)
        findings.extend(
            str(item.get("summary", ""))
            for item in first_normal_form_findings
            if str(item.get("summary", "")).strip()
        )

        if findings:
            first_normal_form_decomposition = self._first_normal_form_decomposition(schema, first_normal_form_findings)
            if first_normal_form_decomposition is not None:
                first_normal_form_decomposition.table_findings = findings
                return first_normal_form_decomposition

            return NormalizationProposal(
                status="candidate_normalization",
                summary="Schema metadata or sampled data suggests at least one table may benefit from normalization review.",
                rationale=[
                    f"Analyzed {len(schema.tables)} tables using schema metadata only.",
                    "This is a heuristic pass; confirming real normal-form violations still needs data- or workload-aware review.",
                ],
                table_findings=findings,
                functional_dependencies=[
                    {
                        "table": str(item.get("table", "")),
                        "determinant": [str(item.get("column", ""))],
                        "dependent": [str(item.get("column", ""))],
                        "confidence": "medium" if float(item.get("confidence", 0.0) or 0.0) >= 0.65 else "low",
                        "reason": str(item.get("summary", "")),
                        "normal_form": "1NF",
                    }
                    for item in first_normal_form_findings
                    if str(item.get("column", "")).strip()
                ],
                source="metadata_fallback",
            )

        if missing_keys:
            return NormalizationProposal(
                status="insufficient_evidence",
                summary="The schema does not show enough key information to assess 3NF confidently.",
                rationale=[
                    "Metadata-only analysis depends on primary-key and unique-constraint evidence.",
                    "Add missing constraints or provide domain guidance before automatic normalization planning.",
                ],
                table_findings=missing_keys,
                source="metadata_fallback",
            )

        return NormalizationProposal(
            status="appears_3nf",
            summary="No obvious 3NF warning signs were found in the metadata-only pass.",
            rationale=[
                f"Analyzed {len(schema.tables)} keyed tables.",
                "This is not a formal proof of 3NF; it is a conservative first pass from schema metadata alone.",
            ],
            source="metadata_fallback",
        )

    def _build_messages(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict],
        *,
        global_catalog: dict | None = None,
        chunk_scope: list[str] | None = None,
    ) -> list[dict[str, str]]:
        schema_payload = {
            "database": schema.database,
            "target_database": self._target_database,
            "tables": [
                {
                    "table": f"{table.schema}.{table.name}",
                    "columns": [
                        {
                            "name": column.name,
                            "data_type": column.data_type,
                            "is_nullable": column.is_nullable,
                            "default": column.default,
                        }
                        for column in table.columns
                    ],
                    "unique_constraints": [constraint.columns for constraint in table.unique_constraints],
                    "foreign_keys": [
                        {
                            "columns": foreign_key.columns,
                            "referenced_table": foreign_key.referenced_table,
                            "referenced_columns": foreign_key.referenced_columns,
                        }
                        for foreign_key in table.foreign_keys
                    ],
                }
                for table in schema.tables
            ],
        }
        system_prompt = (
            "You are a conservative database normalization planner. Treat functional dependencies as valid only when "
            "they are supported by declared primary-key or unique-constraint evidence, or by explicit deterministic "
            "sample evidence supplied in the prompt. Never infer uniqueness from a column name or world knowledge. "
            "When evidence is insufficient, return insufficient_evidence with empty functional_dependencies and "
            "target_tables arrays. Return one complete RFC 8259 JSON object only: no markdown fences, comments, "
            "trailing commas, placeholders, or text outside the JSON object."
        )
        chunk_rules = (
            "15. This is a bounded schema chunk. Analyze only the fully described tables in chunk_scope. "
            "The global catalog is relationship context only; do not propose target columns from catalog-only tables. "
            "Possible associations are unverified name/type matches, not keys, foreign keys, or functional-dependency evidence. "
            "If recommending a decomposition, return a complete target representation of every table in chunk_scope.\n\n"
            if global_catalog is not None
            else ""
        )
        catalog_context = (
            f"Global relationship catalog:\n{json.dumps(global_catalog, ensure_ascii=True, indent=2)}\n\n"
            f"Chunk scope:\n{json.dumps(chunk_scope or [], ensure_ascii=True, indent=2)}\n\n"
            if global_catalog is not None
            else ""
        )
        user_prompt = (
            "Analyze the PostgreSQL schema package below. "
            "If the schema appears already in 1NF/3NF or there is not enough evidence, say so. "
            "If normalization is appropriate, propose a decomposition into target tables.\n\n"
            "Rules:\n"
            "1. Use only source columns that already exist in the schema package.\n"
            "2. Every target table column must identify its source table and source column.\n"
            "3. Preserve source data types by default. Do not change a column's effective data type unless absolutely necessary.\n"
            "4. Foreign-key columns must stay type-compatible with the referenced target columns.\n"
            "5. Keep the proposal deterministic and practical for SQL generation.\n"
            "6. Be explicit about candidate functional dependencies, 1NF warnings, and confidence.\n"
            "7. Prefer concise table names in snake_case.\n\n"
            "8. Do not invent hypothetical columns, lookup descriptions, keys, constraints, or source values.\n"
            "9. Do not claim that an identifier determines event-level timestamps or measurements unless a declared key, "
            "unique constraint, or supplied sample result proves that dependency.\n"
            "10. A data type, repeated categorical code, derived total, readability concern, or possible performance benefit "
            "is not by itself evidence of a 1NF or 3NF violation.\n"
            "11. Every primary-key, unique, and foreign-key column must occur exactly once in its target table. Every "
            "referenced table and referenced column must exist in the proposal.\n"
            "12. If no evidence-supported decomposition exists, set assessment.status to insufficient_evidence, return "
            "empty functional_dependencies and target_tables arrays, and do not propose a decomposition.\n"
            "13. Output strict JSON only. Do not use // comments, block comments, trailing commas, markdown fences, "
            "ellipsis, or explanatory text outside the JSON object. Finish and close the complete JSON object.\n\n"
            "14. Treat deterministic 1NF findings as evidence that a column may contain repeated values inside a single cell. "
            "For delimited multi-value columns, prefer a child relation with the parent key plus one extracted value per row. "
            "When a candidate reference is supplied, use it as a likely foreign-key target if type-compatible.\n\n"
            + chunk_rules
            + "Return JSON with this shape:\n"
            "{\n"
            '  "assessment": {\n'
            '    "status": "appears_3nf|candidate_normalization|insufficient_evidence",\n'
            '    "summary": "short summary",\n'
            '    "reasoning": ["..."]\n'
            "  },\n"
            '  "table_findings": ["..."],\n'
            '  "functional_dependencies": [\n'
            "    {\n"
            '      "table": "public.example",\n'
            '      "determinant": ["col"],\n'
            '      "dependent": ["col"],\n'
            '      "confidence": "high|medium|low",\n'
            '      "reason": "why"\n'
            "    }\n"
            "  ],\n"
            '  "target_tables": [\n'
            "    {\n"
            '      "name": "new_table_name",\n'
            '      "purpose": "why this table exists",\n'
            '      "source_tables": ["public.example"],\n'
            '      "columns": [\n'
            "        {\n"
            '          "source_table": "public.example",\n'
            '          "source_column": "col",\n'
            '          "name": "col"\n'
            "        }\n"
            "      ],\n"
            '      "primary_key": ["col"],\n'
            '      "uniques": [["col"]],\n'
            '      "foreign_keys": [\n'
            "        {\n"
            '          "columns": ["col"],\n'
            '          "references_table": "other_new_table",\n'
            '          "references_columns": ["col"]\n'
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ],\n"
            '  "decomposition_summary": "e.g. normalized from 2 tables to 4 tables"\n'
            "}\n\n"
            + catalog_context
            + f"Schema package:\n{json.dumps(schema_payload, ensure_ascii=True, indent=2)}\n\n"
            + f"Deterministic 1NF findings from sampled data:\n{json.dumps(first_normal_form_findings, ensure_ascii=True, indent=2)}"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _build_fd_discovery_messages(
        self,
        schema: SchemaSnapshot,
        target_tables: list[dict],
        *,
        source_catalog: dict | None = None,
        target_catalog: list[dict] | None = None,
        chunk_scope: list[str] | None = None,
    ) -> list[dict[str, str]]:
        system_prompt = (
            "You discover candidate functional dependencies for database normalization. "
            "Use semantic reasoning conservatively. Return JSON only and do not include markdown fences."
        )
        chunk_rules = (
            "6. This is a table-atomic FD-discovery chunk. Propose dependencies only for the table in chunk_scope. "
            "Catalog-only tables and unverified associations provide context, not dependency or uniqueness evidence.\n\n"
            if source_catalog is not None
            else ""
        )
        catalog_context = (
            f"Global source relationship catalog:\n{json.dumps(source_catalog, ensure_ascii=True, indent=2)}\n\n"
            f"Global 1NF target catalog:\n{json.dumps(target_catalog or [], ensure_ascii=True, indent=2)}\n\n"
            f"Chunk scope:\n{json.dumps(chunk_scope or [], ensure_ascii=True, indent=2)}\n\n"
            if source_catalog is not None
            else f"Original source schema:\n{json.dumps(self._schema_summary(schema), ensure_ascii=True, indent=2)}\n\n"
        )
        user_prompt = (
            "Given the original PostgreSQL schema and the current 1NF target tables, propose candidate functional "
            "dependencies that may justify decomposition to 3NF. Focus on dependencies inside one target table. "
            "Do not propose dependencies where the determinant is already a declared primary key or unique key unless "
            "there is a specific non-obvious reason.\n\n"
            "Rules:\n"
            "1. Use target table names and target column names in each dependency.\n"
            "2. determinant and dependent must be non-empty arrays of columns that exist in the named target table.\n"
            "3. Prefer semantically meaningful dependencies such as code -> description, identifier -> descriptive attributes, "
            "or natural key -> non-key attributes.\n"
            "4. Mark confidence high, medium, or low and explain briefly.\n"
            "5. These are candidates for human approval, not proofs.\n\n"
            + chunk_rules
            + "Return JSON with this shape:\n"
            "{\n"
            '  "status": "candidate_normalization|appears_3nf|insufficient_evidence",\n'
            '  "summary": "short summary",\n'
            '  "reasoning": ["..."],\n'
            '  "table_findings": ["..."],\n'
            '  "functional_dependencies": [\n'
            "    {\n"
            '      "table": "target_table",\n'
            '      "determinant": ["col_a"],\n'
            '      "dependent": ["col_b", "col_c"],\n'
            '      "confidence": "high|medium|low",\n'
            '      "reason": "why this dependency is plausible"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            + catalog_context
            + f"Current 1NF target tables:\n{json.dumps(target_tables, ensure_ascii=True, indent=2)}"
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

    def _build_repair_messages(
        self,
        schema: SchemaSnapshot,
        existing_target_tables: list[dict],
        existing_sql_statements: list[str],
        error_message: str,
    ) -> list[dict[str, str]]:
        schema_payload = {
            "database": schema.database,
            "target_database": self._target_database,
            "tables": [
                {
                    "table": f"{table.schema}.{table.name}",
                    "columns": [
                        {
                            "name": column.name,
                            "data_type": column.data_type,
                            "is_nullable": column.is_nullable,
                            "default": column.default,
                        }
                        for column in table.columns
                    ],
                    "unique_constraints": [constraint.columns for constraint in table.unique_constraints],
                    "foreign_keys": [
                        {
                            "columns": foreign_key.columns,
                            "referenced_table": foreign_key.referenced_table,
                            "referenced_columns": foreign_key.referenced_columns,
                        }
                        for foreign_key in table.foreign_keys
                    ],
                }
                for table in schema.tables
            ],
        }
        system_prompt = (
            "You repair normalized PostgreSQL target-table proposals after CREATE TABLE execution errors. "
            "Return JSON only, with corrected target tables. Do not include markdown fences."
        )
        user_prompt = (
            "The previous normalized target-table proposal failed when executed in PostgreSQL.\n\n"
            "PostgreSQL error:\n"
            f"{error_message}\n\n"
            "Source schema package:\n"
            f"{json.dumps(schema_payload, ensure_ascii=True, indent=2)}\n\n"
            "Existing target tables:\n"
            f"{json.dumps(existing_target_tables, ensure_ascii=True, indent=2)}\n\n"
            "Existing generated SQL:\n"
            f"{json.dumps(existing_sql_statements, ensure_ascii=True, indent=2)}\n\n"
            "Return JSON with this shape:\n"
            "{\n"
            '  "status": "candidate_normalization|appears_3nf|insufficient_evidence",\n'
            '  "summary": "short repair summary",\n'
            '  "reasoning": ["..."],\n'
            '  "table_findings": ["..."],\n'
            '  "functional_dependencies": [],\n'
            '  "target_tables": [\n'
            "    {\n"
            '      "name": "table_name",\n'
            '      "purpose": "why it exists",\n'
            '      "source_tables": ["public.example"],\n'
            '      "columns": [{"source_table": "public.example", "source_column": "col", "name": "col"}],\n'
            '      "primary_key": ["col"],\n'
            '      "uniques": [["col"]],\n'
            '      "foreign_keys": [{"columns": ["col"], "references_table": "other_table", "references_columns": ["col"]}]\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Repair rules:\n"
            "1. Preserve source data types by default. Do not repurpose an integer code column into a text column unless absolutely necessary.\n"
            "2. Foreign-key columns must be type-compatible with the referenced target columns.\n"
            "3. Prefer repairing keys and references over renaming columns into incompatible semantics.\n"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _normalize_functional_dependencies(self, items: list) -> list[dict]:
        normalized: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            determinant = self._to_string_list(item.get("determinant", []))
            dependent = self._to_string_list(item.get("dependent", []))
            if not determinant or not dependent:
                continue
            normalized.append(
                {
                    "table": str(item.get("table", "")),
                    "determinant": determinant,
                    "dependent": dependent,
                    "confidence": str(item.get("confidence", "medium")),
                    "reason": str(item.get("reason", "")),
                }
            )
        return normalized

    def _normalize_target_tables(self, schema: SchemaSnapshot, items: list) -> list[dict]:
        known_columns = {
            (f"{table.schema}.{table.name}", column.name): column
            for table in schema.tables
            for column in table.columns
        }
        target_tables: list[dict] = []
        table_names: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            table_name = self._sanitize_identifier(str(item.get("name", "")).strip())
            if not table_name:
                raise ValueError("LLM target table proposal is missing a valid name.")
            if table_name in table_names:
                raise ValueError(f"Duplicate target table name proposed: {table_name}")
            table_names.add(table_name)

            columns: list[dict] = []
            column_names: set[str] = set()
            for column_item in item.get("columns", []):
                if not isinstance(column_item, dict):
                    continue
                source_table = str(column_item.get("source_table", "")).strip()
                source_column = str(column_item.get("source_column", "")).strip()
                target_column_name = self._sanitize_identifier(
                    str(column_item.get("name") or source_column).strip()
                )
                if (source_table, source_column) not in known_columns:
                    raise ValueError(
                        f"LLM target table {table_name} references unknown source column {source_table}.{source_column}"
                    )
                if not target_column_name:
                    raise ValueError(f"LLM target table {table_name} includes an invalid target column name.")
                if target_column_name in column_names:
                    raise ValueError(f"LLM target table {table_name} repeats target column {target_column_name}.")
                column_names.add(target_column_name)
                columns.append(
                    {
                        "source_table": source_table,
                        "source_column": source_column,
                        "name": target_column_name,
                    }
                )

            if not columns:
                raise ValueError(f"LLM target table {table_name} has no valid columns.")

            primary_key = self._sanitize_identifier_list(item.get("primary_key", []))
            uniques = [self._sanitize_identifier_list(unique) for unique in item.get("uniques", [])]
            foreign_keys = []
            for foreign_key in item.get("foreign_keys", []):
                if not isinstance(foreign_key, dict):
                    continue
                fk_columns = self._sanitize_identifier_list(foreign_key.get("columns", []))
                ref_table = self._sanitize_identifier(str(foreign_key.get("references_table", "")).strip())
                ref_columns = self._sanitize_identifier_list(foreign_key.get("references_columns", []))
                if fk_columns and ref_table and ref_columns:
                    foreign_keys.append(
                        {
                            "columns": fk_columns,
                            "references_table": ref_table,
                            "references_columns": ref_columns,
                        }
                    )

            valid_column_names = {column["name"] for column in columns}
            if primary_key and not set(primary_key).issubset(valid_column_names):
                raise ValueError(f"LLM target table {table_name} uses primary-key columns not present in the table.")
            for unique in uniques:
                if unique and not set(unique).issubset(valid_column_names):
                    raise ValueError(f"LLM target table {table_name} uses unique columns not present in the table.")
            for foreign_key in foreign_keys:
                if not set(foreign_key["columns"]).issubset(valid_column_names):
                    raise ValueError(f"LLM target table {table_name} uses FK columns not present in the table.")

            target_tables.append(
                {
                    "name": table_name,
                    "purpose": str(item.get("purpose", "")),
                    "source_tables": self._to_string_list(item.get("source_tables", [])),
                    "columns": columns,
                    "primary_key": primary_key,
                    "uniques": [unique for unique in uniques if unique],
                    "foreign_keys": foreign_keys,
                }
            )

        known_target_names = {table["name"] for table in target_tables}
        for table in target_tables:
            for foreign_key in table["foreign_keys"]:
                if foreign_key["references_table"] not in known_target_names:
                    raise ValueError(
                        f"LLM target table {table['name']} references unknown target table {foreign_key['references_table']}."
                    )
        self._validate_type_preservation(schema, target_tables, known_columns)
        return target_tables

    def _first_normal_form_decomposition(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict],
    ) -> NormalizationProposal | None:
        split_findings = [
            finding
            for finding in first_normal_form_findings
            if finding.get("pattern") in {"delimited_multi_value_column", "collection_typed_column"}
            and finding.get("table")
            and finding.get("column")
            and (
                finding.get("evidence", {}).get("delimiter")
                or finding.get("evidence", {}).get("collection_kind") == "array"
            )
        ]
        if not split_findings:
            return None

        source_tables = {f"{table.schema}.{table.name}": table for table in schema.tables}
        table_name_map = self._target_table_name_map(schema.tables)
        split_columns_by_table: dict[str, set[str]] = {}
        for finding in split_findings:
            split_columns_by_table.setdefault(str(finding["table"]), set()).add(str(finding["column"]))

        target_tables: list[dict] = []
        declared_primary_keys: dict[str, list[str]] = {}
        for source_table_name, table in source_tables.items():
            excluded_columns = split_columns_by_table.get(source_table_name, set())
            columns = [
                {
                    "source_table": source_table_name,
                    "source_column": column.name,
                    "name": self._sanitize_identifier(column.name),
                }
                for column in table.columns
                if column.name not in excluded_columns
            ]
            if not columns:
                continue
            valid_column_names = {column["name"] for column in columns}
            primary_key = self._first_unique_constraint(table, valid_column_names)
            if primary_key:
                declared_primary_keys[source_table_name] = primary_key
            uniques = [
                self._sanitize_identifier_list(constraint.columns)
                for constraint in table.unique_constraints[1:]
            ]
            target_tables.append(
                {
                    "name": table_name_map[source_table_name],
                    "purpose": f"Copied from {source_table_name}, excluding detected multi-value columns.",
                    "source_tables": [source_table_name],
                    "columns": columns,
                    "primary_key": primary_key,
                    "uniques": [
                        unique
                        for unique in uniques
                        if unique and set(unique).issubset(valid_column_names) and unique != primary_key
                    ],
                    "foreign_keys": [],
                    "migration_strategy": "copy_distinct",
                }
            )

        for index, finding in enumerate(split_findings, start=1):
            source_table_name = str(finding["table"])
            source_column_name = str(finding["column"])
            source_table = source_tables.get(source_table_name)
            if source_table is None:
                continue
            source_target_name = table_name_map.get(source_table_name)
            parent_key = self._parent_key_columns(source_table)
            if not source_target_name or not parent_key:
                continue

            evidence = finding.get("evidence", {})
            candidate_reference = finding.get("candidate_reference") or {}
            reference_table_name = str(candidate_reference.get("table", "")).strip()
            reference_column_name = str(candidate_reference.get("column", "")).strip()
            value_column_name = (
                self._sanitize_identifier(reference_column_name)
                if reference_column_name
                else self._singular_identifier(source_column_name)
            )
            if not value_column_name:
                value_column_name = "value"

            child_table_name = self._unique_target_name(
                f"{source_target_name}_{self._sanitize_identifier(source_column_name)}",
                {table["name"] for table in target_tables},
            )
            columns = [
                {
                    "source_table": source_table_name,
                    "source_column": column_name,
                    "name": self._sanitize_identifier(column_name),
                }
                for column_name in parent_key
            ]
            columns.append(
                {
                    "source_table": source_table_name,
                    "source_column": source_column_name,
                    "name": value_column_name,
                    **(
                        {"data_type": self._array_element_type(str(evidence.get("data_type", "")))}
                        if evidence.get("collection_kind") == "array"
                        else {}
                    ),
                }
            )
            foreign_keys = []
            sanitized_parent_key = [self._sanitize_identifier(column_name) for column_name in parent_key]
            if declared_primary_keys.get(source_table_name) == sanitized_parent_key:
                foreign_keys.append(
                    {
                        "columns": sanitized_parent_key,
                        "references_table": source_target_name,
                        "references_columns": sanitized_parent_key,
                    }
                )
            referenced_target_name = table_name_map.get(reference_table_name)
            if (
                referenced_target_name
                and reference_column_name
                and self._target_has_unique_columns(
                    target_tables,
                    referenced_target_name,
                    [self._sanitize_identifier(reference_column_name)],
                )
            ):
                foreign_keys.append(
                    {
                        "columns": [value_column_name],
                        "references_table": referenced_target_name,
                        "references_columns": [self._sanitize_identifier(reference_column_name)],
                    }
                )

            target_tables.append(
                {
                    "name": child_table_name,
                    "purpose": f"One row per value extracted from {source_table_name}.{source_column_name}.",
                    "source_tables": [source_table_name],
                    "columns": columns,
                    "primary_key": [self._sanitize_identifier(column_name) for column_name in parent_key] + [value_column_name],
                    "uniques": [],
                    "foreign_keys": foreign_keys,
                    "migration_strategy": (
                        "unnest_array" if evidence.get("collection_kind") == "array" else "split_delimited"
                    ),
                    "split_source_column": source_column_name,
                    "split_value_column": value_column_name,
                    "split_delimiter": str(evidence.get("delimiter", ",")),
                    "normal_form_finding_index": index,
                }
            )

        if not target_tables:
            return None
        sql_statements = self._generate_sql(target_tables, schema)
        finding_summaries = [
            str(finding.get("summary", ""))
            for finding in split_findings
            if str(finding.get("summary", "")).strip()
        ]
        return NormalizationProposal(
            status="candidate_normalization",
            summary=(
                f"Generated deterministic 1NF decomposition with {len(target_tables)} target table(s), "
                f"including {len(split_findings)} child table(s) for multi-value columns."
            ),
            target_database=self._target_database,
            rationale=[
                "Generated a conservative schema path from deterministic 1NF warning findings.",
                "Original tables are copied with detected multi-value columns removed; child tables hold one extracted value per row.",
            ],
            table_findings=finding_summaries,
            functional_dependencies=[
                {
                    "table": str(finding.get("table", "")),
                    "determinant": [str(finding.get("column", ""))],
                    "dependent": [str(finding.get("column", ""))],
                    "confidence": "medium" if float(finding.get("confidence", 0.0) or 0.0) >= 0.65 else "low",
                    "reason": str(finding.get("summary", "")),
                    "normal_form": "1NF",
                }
                for finding in split_findings
            ],
            target_tables=target_tables,
            sql_statements=sql_statements,
            source="deterministic_1nf_fallback",
        )

    def _synthesize_3nf_tables(self, target_tables: list[dict], functional_dependencies: list[dict]) -> list[dict]:
        tables_by_name = {str(table.get("name", "")): self._copy_target_table(table) for table in target_tables}
        used_names = set(tables_by_name)
        fds_by_table: dict[str, list[dict]] = {}
        for fd in functional_dependencies:
            table_name = str(fd.get("table", "")).strip()
            if table_name in tables_by_name:
                fds_by_table.setdefault(table_name, []).append(fd)

        synthesized: list[dict] = []
        for table_name, table in tables_by_name.items():
            original_columns = [dict(column) for column in table.get("columns", [])]
            original_column_names = {column["name"] for column in original_columns}
            primary_key = list(table.get("primary_key", []))
            unique_sets = [list(unique) for unique in table.get("uniques", [])]
            key_sets = [set(primary_key)] if primary_key else []
            key_sets.extend(set(unique) for unique in unique_sets if unique)
            remove_columns: set[str] = set()

            for fd in fds_by_table.get(table_name, []):
                determinant = [
                    self._sanitize_identifier(str(column))
                    for column in fd.get("determinant", [])
                    if self._sanitize_identifier(str(column)) in original_column_names
                ]
                dependent = [
                    self._sanitize_identifier(str(column))
                    for column in fd.get("dependent", [])
                    if self._sanitize_identifier(str(column)) in original_column_names
                ]
                dependent = [column for column in dependent if column not in determinant]
                if not determinant or not dependent:
                    continue
                determinant_set = set(determinant)
                if any(key and determinant_set.issuperset(key) for key in key_sets):
                    continue

                relation_columns = self._columns_by_name(original_columns, determinant + dependent)
                relation_name = self._unique_target_name(
                    f"{table_name}_{'_'.join(dependent[:2])}",
                    used_names,
                )
                used_names.add(relation_name)
                synthesized.append(
                    {
                        "name": relation_name,
                        "purpose": f"3NF relation for {table_name}: {', '.join(determinant)} determines {', '.join(dependent)}.",
                        "source_tables": list(table.get("source_tables", [])),
                        "columns": relation_columns,
                        "primary_key": determinant,
                        "uniques": [],
                        "foreign_keys": [],
                        "normal_form": "3NF",
                        "functional_dependency": fd,
                    }
                )
                for column in dependent:
                    if column not in primary_key:
                        remove_columns.add(column)
                if determinant_set.issubset(original_column_names):
                    table.setdefault("foreign_keys", []).append(
                        {
                            "columns": determinant,
                            "references_table": relation_name,
                            "references_columns": determinant,
                        }
                    )

            if remove_columns:
                table["columns"] = [
                    column
                    for column in table.get("columns", [])
                    if column.get("name") not in remove_columns
                ]
                valid_names = {column["name"] for column in table["columns"]}
                table["primary_key"] = [column for column in table.get("primary_key", []) if column in valid_names]
                table["uniques"] = [
                    [column for column in unique if column in valid_names]
                    for unique in table.get("uniques", [])
                    if set(unique).issubset(valid_names)
                ]
                table["foreign_keys"] = [
                    foreign_key
                    for foreign_key in table.get("foreign_keys", [])
                    if set(foreign_key.get("columns", [])).issubset(valid_names)
                ]
            table["normal_form"] = "3NF" if fds_by_table.get(table_name) else table.get("normal_form", "1NF")

        result = list(tables_by_name.values()) + synthesized
        return [table for table in result if table.get("columns")]

    def _copy_target_table(self, table: dict) -> dict:
        return {
            "name": str(table.get("name", "")),
            "purpose": str(table.get("purpose", "")),
            "source_tables": list(table.get("source_tables", [])),
            "columns": [dict(column) for column in table.get("columns", [])],
            "primary_key": list(table.get("primary_key", [])),
            "uniques": [list(unique) for unique in table.get("uniques", [])],
            "foreign_keys": [dict(foreign_key) for foreign_key in table.get("foreign_keys", [])],
            **{
                key: value
                for key, value in table.items()
                if key
                not in {
                    "name",
                    "purpose",
                    "source_tables",
                    "columns",
                    "primary_key",
                    "uniques",
                    "foreign_keys",
                }
            },
        }

    def _columns_by_name(self, columns: list[dict], names: list[str]) -> list[dict]:
        by_name = {column.get("name"): dict(column) for column in columns}
        return [by_name[name] for name in names if name in by_name]

    def _generate_sql(self, target_tables: list[dict], schema: SchemaSnapshot) -> list[str]:
        if not target_tables:
            return []
        ordered_target_tables = self._order_target_tables(target_tables)
        known_columns = {
            (f"{table.schema}.{table.name}", column.name): column
            for table in schema.tables
            for column in table.columns
        }
        statements: list[str] = []
        for table in ordered_target_tables:
            definitions: list[str] = []
            for column in table["columns"]:
                source = known_columns[(column["source_table"], column["source_column"])]
                nullable = source.is_nullable and column["name"] not in table["primary_key"]
                null_sql = "" if nullable else " NOT NULL"
                default_sql = f" DEFAULT {source.default}" if source.default else ""
                target_data_type = str(column.get("data_type") or source.data_type)
                definitions.append(
                    f'  "{column["name"]}" {target_data_type.upper()}{default_sql}{null_sql}'
                )

            if table["primary_key"]:
                pk_columns = ", ".join(f'"{column}"' for column in table["primary_key"])
                definitions.append(f"  PRIMARY KEY ({pk_columns})")
            for unique in table["uniques"]:
                unique_columns = ", ".join(f'"{column}"' for column in unique)
                definitions.append(f"  UNIQUE ({unique_columns})")
            for foreign_key in table["foreign_keys"]:
                fk_columns = ", ".join(f'"{column}"' for column in foreign_key["columns"])
                ref_columns = ", ".join(f'"{column}"' for column in foreign_key["references_columns"])
                definitions.append(
                    f'  FOREIGN KEY ({fk_columns}) REFERENCES "{foreign_key["references_table"]}" ({ref_columns})'
                )

            body = ",\n".join(definitions)
            statements.append(f'CREATE TABLE IF NOT EXISTS "{table["name"]}" (\n{body}\n);')
        return statements

    def _order_target_tables(self, target_tables: list[dict]) -> list[dict]:
        remaining = {table["name"]: table for table in target_tables}
        ordered: list[dict] = []
        while remaining:
            ready = [
                table
                for table in remaining.values()
                if all(
                    foreign_key["references_table"] not in remaining
                    for foreign_key in table.get("foreign_keys", [])
                )
            ]
            if not ready:
                return target_tables
            for table in sorted(ready, key=lambda item: item["name"]):
                ordered.append(table)
                remaining.pop(table["name"], None)
        return ordered

    def _sanitize_identifier(self, value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()

    def _sanitize_identifier_list(self, items: list) -> list[str]:
        return [identifier for identifier in (self._sanitize_identifier(str(item)) for item in items) if identifier]

    def _target_table_name_map(self, tables) -> dict[str, str]:
        raw_names = [table.name for table in tables]
        duplicate_names = {name for name in raw_names if raw_names.count(name) > 1}
        used: set[str] = set()
        result: dict[str, str] = {}
        for table in tables:
            source_table_name = f"{table.schema}.{table.name}"
            base_name = f"{table.schema}_{table.name}" if table.name in duplicate_names else table.name
            result[source_table_name] = self._unique_target_name(base_name, used)
        return result

    def _unique_target_name(self, value: str, used: set[str]) -> str:
        base = self._sanitize_identifier(value)[:58] or "table"
        candidate = base
        suffix = 2
        while candidate in used:
            suffix_text = f"_{suffix}"
            candidate = f"{base[:63 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        used.add(candidate)
        return candidate

    def _first_unique_constraint(self, table, valid_column_names: set[str]) -> list[str]:
        for constraint in table.unique_constraints:
            columns = self._sanitize_identifier_list(constraint.columns)
            if columns and set(columns).issubset(valid_column_names):
                return columns
        return []

    def _parent_key_columns(self, table) -> list[str]:
        for constraint in table.unique_constraints:
            if constraint.columns:
                return list(constraint.columns)
        likely_columns = [
            column.name
            for column in table.columns
            if re.search(r"(^id$|_id$|const$|code$|key$)", column.name, re.IGNORECASE)
        ]
        if likely_columns:
            return [likely_columns[0]]
        if table.columns:
            return [table.columns[0].name]
        return []

    def _singular_identifier(self, value: str) -> str:
        identifier = self._sanitize_identifier(value)
        if identifier.endswith("ies") and len(identifier) > 3:
            return identifier[:-3] + "y"
        if identifier.endswith("s") and len(identifier) > 1:
            return identifier[:-1]
        return identifier

    def _target_has_unique_columns(self, target_tables: list[dict], table_name: str, columns: list[str]) -> bool:
        for table in target_tables:
            if table.get("name") != table_name:
                continue
            if table.get("primary_key") == columns:
                return True
            return any(unique == columns for unique in table.get("uniques", []))
        return False

    def _validate_type_preservation(
        self,
        schema: SchemaSnapshot,
        target_tables: list[dict],
        known_columns: dict[tuple[str, str], object],
    ) -> None:
        target_column_types: dict[tuple[str, str], str] = {}
        source_table_column_types: dict[str, dict[str, str]] = {}

        for table in schema.tables:
            source_table_column_types[f"{table.schema}.{table.name}"] = {
                column.name: self._canonical_data_type(column.data_type)
                for column in table.columns
            }

        for table in target_tables:
            source_tables = set(table.get("source_tables", []))
            for column in table["columns"]:
                source = known_columns[(column["source_table"], column["source_column"])]
                target_type = self._canonical_data_type(source.data_type)
                target_column_types[(table["name"], column["name"])] = target_type

                if column["source_column"] == column["name"]:
                    continue

                same_named_source_types = {
                    source_table_column_types[source_table][column["name"]]
                    for source_table in source_tables
                    if column["name"] in source_table_column_types.get(source_table, {})
                }
                if same_named_source_types and target_type not in same_named_source_types:
                    raise ValueError(
                        f"LLM target column {table['name']}.{column['name']} would change the effective type of an "
                        f"existing source column name by mapping {column['source_table']}.{column['source_column']} "
                        f"to {column['name']}."
                    )

        for table in target_tables:
            for foreign_key in table["foreign_keys"]:
                if len(foreign_key["columns"]) != len(foreign_key["references_columns"]):
                    raise ValueError(
                        f"LLM target table {table['name']} has a foreign key with mismatched column counts."
                    )
                for local_column, referenced_column in zip(
                    foreign_key["columns"],
                    foreign_key["references_columns"],
                    strict=True,
                ):
                    local_type = target_column_types.get((table["name"], local_column))
                    referenced_type = target_column_types.get((foreign_key["references_table"], referenced_column))
                    if local_type is None or referenced_type is None:
                        raise ValueError(
                            f"LLM target table {table['name']} references missing foreign-key columns."
                        )
                    if local_type != referenced_type:
                        raise ValueError(
                            f"LLM target table {table['name']} uses a foreign key with incompatible types: "
                            f"{local_column} ({local_type}) -> {foreign_key['references_table']}.{referenced_column} "
                            f"({referenced_type})."
                        )

    def _array_element_type(self, value: str) -> str:
        normalized = value.strip()
        return normalized[:-2].strip() if normalized.endswith("[]") else normalized

    def _canonical_data_type(self, value: str) -> str:
        normalized = re.sub(r"\s+", " ", value.strip().lower())
        aliases = {
            "int": "integer",
            "int4": "integer",
            "int8": "bigint",
            "serial4": "integer",
            "serial8": "bigint",
            "varchar": "character varying",
            "bool": "boolean",
            "float8": "double precision",
        }
        return aliases.get(normalized, normalized)

    def _to_string_list(self, value: list | str | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value if str(item).strip()]

    def _find_numbered_groups(self, columns: list[str]) -> set[str]:
        groups: dict[str, set[str]] = {}
        for column in columns:
            match = re.match(r"^(.*?)(?:_)?(\d+)$", column)
            if not match:
                continue
            prefix = match.group(1)
            groups.setdefault(prefix, set()).add(column)
        return {prefix for prefix, values in groups.items() if len(values) >= 2}
