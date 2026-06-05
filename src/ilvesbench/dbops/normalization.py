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

    def functional_dependency_review_candidates(
        self,
        *,
        target_tables: list[dict],
        functional_dependencies: list[dict],
        validate_fd,
    ) -> list[dict]:
        target_by_name = {str(table.get("name", "")): table for table in target_tables}
        reviews: list[dict] = []
        for index, fd in enumerate(functional_dependencies, start=1):
            table_name = str(fd.get("table", "")).strip()
            table = target_by_name.get(table_name)
            if table is None:
                continue
            columns_by_name = {
                str(column.get("name", "")): column
                for column in table.get("columns", [])
                if str(column.get("name", "")).strip()
            }
            determinant = [
                str(column).strip()
                for column in fd.get("determinant", [])
                if str(column).strip() in columns_by_name
            ]
            dependent = [
                str(column).strip()
                for column in fd.get("dependent", [])
                if str(column).strip() in columns_by_name and str(column).strip() not in determinant
            ]
            if not determinant or not dependent:
                continue
            validation = self._validate_fd_against_source(
                determinant,
                dependent,
                columns_by_name,
                validate_fd,
            )
            review_id = re.sub(
                r"[^a-zA-Z0-9_]+",
                "_",
                f"fd_{table_name}_{'_'.join(determinant)}_{'_'.join(dependent)}_{index}",
            ).strip("_").lower()
            reviews.append(
                {
                    "id": review_id,
                    "review_type": "3nf_fd",
                    "source_table": table_name,
                    "status": "pending",
                    "normal_forms": ["3NF"],
                    "suspicious_columns": dependent,
                    "determinant": determinant,
                    "dependent": dependent,
                    "functional_dependency": {
                        **fd,
                        "table": table_name,
                        "determinant": determinant,
                        "dependent": dependent,
                    },
                    "confidence": str(fd.get("confidence", "medium")),
                    "proposal_summary": (
                        f"Treat {table_name}.({', '.join(determinant)}) as determining "
                        f"{', '.join(dependent)} and decompose toward 3NF."
                    ),
                    "suspicion_reasons": [
                        str(fd.get("reason", "Candidate functional dependency proposed for 3NF review.")),
                        validation.get("summary", ""),
                    ],
                    "source_data_validation": validation,
                    "proposed_target_tables": [],
                }
            )
        return reviews

    def _validate_fd_against_source(
        self,
        determinant: list[str],
        dependent: list[str],
        columns_by_name: dict[str, dict],
        validate_fd,
    ) -> dict:
        source_columns = [columns_by_name[column] for column in determinant + dependent]
        source_tables = {str(column.get("source_table", "")).strip() for column in source_columns}
        if len(source_tables) != 1 or not next(iter(source_tables), ""):
            return {
                "status": "skipped",
                "summary": "Source-data FD check was skipped because the candidate spans multiple source tables.",
            }
        source_table = next(iter(source_tables))
        if "." not in source_table:
            return {
                "status": "skipped",
                "summary": "Source-data FD check was skipped because the source table is not schema-qualified.",
            }
        schema_name, table_name = source_table.split(".", 1)
        source_by_target = {
            target_name: str(column.get("source_column", "")).strip()
            for target_name, column in columns_by_name.items()
        }
        determinant_source = [source_by_target[column] for column in determinant if source_by_target.get(column)]
        dependent_source = [source_by_target[column] for column in dependent if source_by_target.get(column)]
        if len(determinant_source) != len(determinant) or len(dependent_source) != len(dependent):
            return {
                "status": "skipped",
                "summary": "Source-data FD check was skipped because not every target column has a source mapping.",
            }
        return validate_fd(schema_name, table_name, determinant_source, dependent_source)

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
