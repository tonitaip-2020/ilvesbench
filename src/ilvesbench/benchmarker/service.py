from __future__ import annotations

from pathlib import Path
import re
from typing import Callable

from ilvesbench.benchmark.energy import BenchmarkComparator, EnergyEstimator
from ilvesbench.benchmark.index_advisor import IndexAdvisor
from ilvesbench.benchmark.pgbench import PgBenchParameterAdvisor, PgBenchRunner
from ilvesbench.benchmark.tuning import PostgresTuningAdvisor
from ilvesbench.benchmark.workload import WorkloadPlanner
from ilvesbench.benchmarker.query_rewrite import QueryRewriteService
from ilvesbench.config import IlvesBenchConfig
from ilvesbench.llm.gateway import LLMGateway


class BenchmarkerService:
    """Benchmarking boundary for workload generation and pgbench execution."""

    def __init__(
        self,
        config: IlvesBenchConfig,
        *,
        llm: LLMGateway | None = None,
        pgbench: PgBenchRunner | None = None,
    ) -> None:
        self.pgbench = pgbench or PgBenchRunner()
        self.pgbench_advisor = PgBenchParameterAdvisor()
        self.query_rewrite = QueryRewriteService(config)
        self.query_migration = self.query_rewrite
        self.workload_planner = WorkloadPlanner(
            llm=llm,
            target_database=config.postgres.new_database,
        )
        self.index_advisor = IndexAdvisor(llm=llm)
        self.tuning_advisor = PostgresTuningAdvisor()
        self.energy_estimator = EnergyEstimator()
        self.benchmark_comparator = BenchmarkComparator()

    def prepare_validated_workload(
        self,
        *,
        run_id: str,
        workload_path: Path,
        target_database: str,
        validate_statements: Callable[[list[str]], list[dict]],
        write_text_artifact: Callable[[str, str, str], str],
    ) -> tuple[Path, dict | None]:
        """Validate a rewritten workload and filter invalid statements.

        This is a benchmarker concern because it decides which SQL file pgbench
        should receive. The database-specific validation itself is supplied by
        DBOps via ``validate_statements``.
        """

        if not workload_path.exists():
            return workload_path, None
        statements = self.split_sql_statements(workload_path.read_text(encoding="utf-8", errors="replace"))
        validation_errors = validate_statements(statements)
        if not validation_errors:
            return workload_path, {
                "status": "passed",
                "summary": f"Rewritten workload dry-run validates against {target_database}.",
                "statement_count": len(statements),
                "valid_statement_count": len(statements),
                "error_count": 0,
                "errors": [],
                "blocking": False,
            }

        invalid_indexes = {
            int(item["index"])
            for item in validation_errors
            if str(item.get("index", "")).isdigit()
        }
        invalid_texts = {
            self.normalize_sql_for_compare(str(item.get("statement", "")))
            for item in validation_errors
            if item.get("statement")
        }
        valid_statements = [
            statement
            for index, statement in enumerate(statements)
            if index not in invalid_indexes
            and self.normalize_sql_for_compare(statement) not in invalid_texts
        ]
        if not valid_statements:
            raise ValueError(
                f"None of the rewritten {target_database} workload statements passed "
                "PostgreSQL dry-run validation, so pgbench was not started."
            )

        filtered_path = Path(
            write_text_artifact(
                run_id,
                "rewritten_workload_db_new_validated",
                "\n\n".join(valid_statements) + "\n",
            )
        )
        return filtered_path, {
            "status": "warning",
            "summary": (
                f"{len(validation_errors)} rewritten workload statement(s) failed PostgreSQL dry-run validation "
                f"against {target_database}. pgbench will use {len(valid_statements)} validated statement(s)."
            ),
            "statement_count": len(statements),
            "valid_statement_count": len(valid_statements),
            "error_count": len(validation_errors),
            "errors": validation_errors[:10],
            "filtered_workload_path": str(filtered_path),
            "blocking": False,
        }

    def split_sql_statements(self, text: str) -> list[str]:
        cleaned_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--") or stripped.startswith("\\"):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines)
        return [
            statement.strip() + ";"
            for statement in cleaned.split(";")
            if statement.strip()
        ]

    def normalize_sql_for_compare(self, statement: str) -> str:
        return re.sub(r"\s+", " ", statement.strip().rstrip(";")).strip().lower()
