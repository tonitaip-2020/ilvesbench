from __future__ import annotations

from dataclasses import dataclass
import re

from ilvesbench.models import SchemaSnapshot


@dataclass(frozen=True)
class TargetSchemaPlan:
    status: str
    details: dict
    sql_statements: list[str]


class NormalizationWorkflow:
    """DBOps-side normalization workflow helpers.

    These helpers do not call the LLM. They organize database-derived findings
    and target-schema execution plans so the orchestrator can focus on workflow
    state and LLM mediation.
    """

    def review_candidates(
        self,
        schema: SchemaSnapshot,
        first_normal_form_findings: list[dict],
        draft_target_tables: list[dict],
    ) -> list[dict]:
        reviewable_findings = [
            finding
            for finding in first_normal_form_findings
            if finding.get("pattern") == "delimited_multi_value_column"
            and str(finding.get("table", "")).strip()
            and str(finding.get("column", "")).strip()
        ]
        if not reviewable_findings:
            return []

        source_tables = {f"{table.schema}.{table.name}": table for table in schema.tables}
        findings_by_table: dict[str, list[dict]] = {}
        for finding in reviewable_findings:
            table_name = str(finding.get("table", "")).strip()
            findings_by_table.setdefault(table_name, []).append(finding)

        candidates: list[dict] = []
        for table_name in sorted(findings_by_table):
            findings = findings_by_table[table_name]
            source_table = source_tables.get(table_name)
            columns = [str(finding.get("column", "")) for finding in findings if str(finding.get("column", ""))]
            relevant_targets = [
                table
                for table in draft_target_tables
                if table_name in table.get("source_tables", [])
            ]
            reasons = [
                str(finding.get("summary", ""))
                for finding in findings
                if str(finding.get("summary", "")).strip()
            ]
            if not reasons:
                reasons = [f"{table_name} has sampled data that looks suspicious for normalization review."]

            candidate_id = re.sub(r"[^a-zA-Z0-9_]+", "_", table_name).strip("_").lower()
            candidates.append(
                {
                    "id": candidate_id,
                    "source_table": table_name,
                    "status": "pending",
                    "normal_forms": ["1NF"],
                    "suspicious_columns": columns,
                    "source_column_count": len(source_table.columns) if source_table is not None else 0,
                    "suspicion_reasons": reasons,
                    "proposal_summary": (
                        f"Split {', '.join(columns)} out of {table_name} into child table(s), "
                        "while keeping the remaining columns in a copied parent table."
                    ),
                    "findings": findings,
                    "proposed_target_tables": relevant_targets,
                }
            )
        return candidates

    def target_schema_plan(self, normalization_details: dict, target_database: str) -> TargetSchemaPlan:
        sql_statements = list(normalization_details.get("sql_statements", []))
        target_tables = list(normalization_details.get("target_tables", []))
        summary = str(normalization_details.get("summary", ""))

        if normalization_details.get("review_status") == "pending":
            return TargetSchemaPlan(
                status="planned",
                details={
                    "summary": "Target-schema DDL is waiting for table-by-table normalization review decisions.",
                    "normalization_summary": summary,
                    "pending_review_count": sum(
                        1
                        for review in normalization_details.get("normalization_reviews", [])
                        if review.get("status") == "pending"
                    ),
                },
                sql_statements=[],
            )

        if sql_statements:
            return TargetSchemaPlan(
                status="planned",
                details={
                    "summary": f"Approval-gated SQL is ready for {target_database} schema creation.",
                    "normalization_summary": summary,
                    "target_table_count": len(target_tables),
                    "sql_statement_count": len(sql_statements),
                    "sql_statements": sql_statements,
                },
                sql_statements=sql_statements,
            )

        return TargetSchemaPlan(
            status="planned",
            details={
                "summary": "No target-schema SQL was generated because the proposal did not recommend a decomposition.",
                "normalization_summary": summary,
            },
            sql_statements=[],
        )

