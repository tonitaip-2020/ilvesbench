from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Callable, Iterable

from ilvesbench.benchmark.workload import WorkloadRewriteError
from ilvesbench.config import IlvesBenchConfig


DEFAULT_QUERY_REWRITE_BATCH_SIZE = 25


@dataclass(slots=True)
class QueryMigrationRunResult:
    progress: dict
    ordered_results: list[dict]
    ordered_statements: list[str]
    invalid_count: int


class QueryMigrationExecutionError(WorkloadRewriteError):
    def __init__(
        self,
        message: str,
        *,
        progress: dict,
        raw_response_text: str = "",
        request_payload: dict | None = None,
    ) -> None:
        super().__init__(message, raw_response_text=raw_response_text, request_payload=request_payload)
        self.progress = progress


class QueryMigrationService:
    """Bookkeeping for source-to-target workload query migration."""

    def __init__(self, config: IlvesBenchConfig, *, batch_size: int = DEFAULT_QUERY_REWRITE_BATCH_SIZE) -> None:
        self._config = config
        self.batch_size = batch_size

    def progress_payload(
        self,
        workload_sql: str,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
    ) -> dict:
        return {
            "status": "running",
            "summary": (
                f"Rewriting {len(source_statements)} query statement(s) for "
                f"{self._config.postgres.new_database} in batches of up to {self.batch_size}."
            ),
            "mode": "incremental",
            "total_query_count": len(source_statements),
            "completed_query_count": 0,
            "processed_query_count": 0,
            "cache_hit_count": 0,
            "current_query_index": 0,
            "current_stage": "starting",
            "statement_count": 0,
            "statements": [],
            "query_results": [],
            "raw_responses": [],
            "raw_response_text": "",
            "llm_requests": [],
            "llm_request": {
                "mode": "incremental",
                "total_query_count": len(source_statements),
                "batch_size": self.batch_size,
                "batches": [],
            },
            "source_query_text": workload_sql,
            "target_tables": target_tables,
            "target_table_count": len(target_tables),
            "migration_statements": migration_statements,
        }

    def artifact_payload(self, progress: dict) -> dict:
        statements = self.valid_progress_statements(progress)
        artifact_payload = dict(progress)
        artifact_payload["statement_count"] = len(statements)
        artifact_payload["completed_query_count"] = len(statements)
        artifact_payload["raw_response_text"] = self.raw_response_text(progress)
        artifact_payload["llm_request"] = self.request_summary(
            progress,
            int(progress.get("total_query_count", 0) or 0),
        )
        return artifact_payload

    def request_summary(self, progress: dict, total_query_count: int) -> dict:
        return {
            "mode": "incremental",
            "total_query_count": total_query_count,
            "batch_size": self.batch_size,
            "batches": progress.get("llm_requests", []),
        }

    def batches(self, pending: list[tuple[int, str, str]]) -> list[list[tuple[int, str, str]]]:
        return [
            pending[index : index + self.batch_size]
            for index in range(0, len(pending), self.batch_size)
        ]

    def rewrite_incremental(
        self,
        *,
        workload_sql: str,
        source_statements: list[str],
        target_tables: list[dict],
        migration_statements: list[dict],
        rewrite_batch: Callable[[str, list[dict], list[dict], int], object],
        validate_statement: Callable[[str], dict],
        on_progress: Callable[[dict], None],
    ) -> QueryMigrationRunResult:
        """Rewrite workload statements in batches, with cache reuse and progress callbacks."""

        progress = self.progress_payload(
            workload_sql,
            source_statements,
            target_tables,
            migration_statements,
        )
        on_progress(progress)
        cache = self.load_cache()

        try:
            pending: list[tuple[int, str, str]] = []
            for index, source_statement in enumerate(source_statements, start=1):
                progress["current_query_index"] = index
                progress["current_stage"] = "checking rewrite cache"
                on_progress(progress)

                cache_key = self.cache_key(source_statement, target_tables)
                cached = cache.get(cache_key)
                if cached:
                    query_result = dict(cached)
                    rewritten_statement = str(query_result.get("rewritten_statement", "")).strip()
                    validation = validate_statement(rewritten_statement)
                    if validation.get("status") == "failed":
                        cache.pop(cache_key, None)
                        self.save_cache(cache)
                        pending.append((index, source_statement, cache_key))
                        continue
                    query_result.update({
                        "query_index": index,
                        "cache_hit": True,
                        "stage": "cached",
                        "validation": validation,
                    })
                    progress["query_results"].append(query_result)
                    progress["statements"].append(rewritten_statement)
                    progress["completed_query_count"] = len(progress["statements"])
                    progress["processed_query_count"] = len(progress["query_results"])
                    progress["cache_hit_count"] = int(progress.get("cache_hit_count", 0)) + 1
                    progress["current_stage"] = "cached rewrite reused"
                    on_progress(progress)
                    continue
                pending.append((index, source_statement, cache_key))

            for batch_number, pending_batch in enumerate(self.batches(pending), start=1):
                progress["current_query_index"] = pending_batch[0][0]
                progress["current_stage"] = (
                    f"sending rewrite batch {batch_number} "
                    f"({len(pending_batch)} query statement(s)) to the LLM"
                )
                on_progress(progress)
                pending_sql = "\n\n".join(statement for _, statement, _ in pending_batch)
                proposal = rewrite_batch(
                    pending_sql,
                    target_tables,
                    migration_statements,
                    self.batch_size,
                )
                proposal_statements = list(getattr(proposal, "statements", []))
                if len(proposal_statements) != len(pending_batch):
                    raise WorkloadRewriteError(
                        (
                            f"The LLM returned {len(proposal_statements)} rewritten statement(s), "
                            f"but IlvesBench expected {len(pending_batch)} for batch {batch_number}."
                        ),
                        raw_response_text=str(getattr(proposal, "raw_response_text", "") or ""),
                        request_payload=dict(getattr(proposal, "request_payload", {}) or {}),
                    )
                request_payload = dict(getattr(proposal, "request_payload", {}) or {})
                raw_response_text = str(getattr(proposal, "raw_response_text", "") or "")
                if request_payload:
                    progress["llm_requests"].append(request_payload)
                if raw_response_text:
                    progress["raw_responses"].append(f"Rewrite batch {batch_number} response:\n{raw_response_text}")

                for offset, (index, source_statement, cache_key) in enumerate(pending_batch):
                    rewritten_statement = proposal_statements[offset]
                    progress["current_query_index"] = index
                    progress["current_stage"] = f"validating rewritten query {index} against {self._config.postgres.new_database}"
                    progress["last_rewritten_statement"] = rewritten_statement
                    on_progress(progress)
                    validation = validate_statement(rewritten_statement)
                    validation_passed = validation.get("status") == "passed"
                    query_result = {
                        "query_index": index,
                        "source_statement": source_statement,
                        "rewritten_statement": rewritten_statement,
                        "status": "completed" if validation_passed else "failed",
                        "stage": "target_validation_passed" if validation_passed else "target_validation_failed",
                        "source": getattr(proposal, "source", ""),
                        "reasoning": getattr(proposal, "rationale", []),
                        "cache_hit": False,
                        "validation": validation,
                        "raw_response_text": raw_response_text,
                        "llm_request": request_payload,
                    }
                    progress["query_results"].append(query_result)
                    progress["processed_query_count"] = len(progress["query_results"])
                    if validation_passed:
                        progress["statements"].append(rewritten_statement)
                        progress["completed_query_count"] = len(progress["statements"])
                        progress["current_stage"] = f"query {index} validated and saved"
                        cache[cache_key] = {
                            "source_statement": source_statement,
                            "rewritten_statement": rewritten_statement,
                            "source": getattr(proposal, "source", ""),
                            "reasoning": getattr(proposal, "rationale", []),
                            "validation": validation,
                            "target_table_count": len(target_tables),
                            "saved_at": datetime.now(UTC).isoformat(),
                        }
                        self.save_cache(cache)
                    else:
                        progress["current_stage"] = f"query {index} failed target validation"
                    on_progress(progress)

            ordered_results, ordered_statements, invalid_count = self.complete_progress(progress)
            if not ordered_statements:
                raise WorkloadRewriteError(
                    (
                        f"None of the {len(source_statements)} rewritten query statement(s) validated against "
                        f"{self._config.postgres.new_database}; no pgbench workload was generated."
                    ),
                    raw_response_text=self.raw_response_text(progress),
                    request_payload=self.request_summary(progress, len(source_statements)),
                )
            return QueryMigrationRunResult(
                progress=progress,
                ordered_results=ordered_results,
                ordered_statements=ordered_statements,
                invalid_count=invalid_count,
            )
        except WorkloadRewriteError as exc:
            if exc.raw_response_text and exc.raw_response_text not in self.raw_response_text(progress):
                progress["raw_responses"].append(exc.raw_response_text)
            raise QueryMigrationExecutionError(
                str(exc),
                progress=progress,
                raw_response_text=exc.raw_response_text,
                request_payload=exc.request_payload or self.request_summary(progress, len(source_statements)),
            ) from exc
        except Exception as exc:
            raise QueryMigrationExecutionError(
                str(exc),
                progress=progress,
                raw_response_text=self.raw_response_text(progress),
                request_payload=self.request_summary(progress, len(source_statements)),
            ) from exc

    def ordered_results(self, progress: dict) -> list[dict]:
        results = [item for item in progress.get("query_results", []) if isinstance(item, dict)]
        return sorted(results, key=lambda item: int(item.get("query_index", 0) or 0))

    def valid_rewritten_statements(self, query_results: Iterable[dict]) -> list[str]:
        statements: list[str] = []
        for item in query_results:
            statement = str(item.get("rewritten_statement", "")).strip()
            if statement and item.get("validation", {}).get("status") == "passed":
                statements.append(statement)
        return statements

    def valid_progress_statements(self, progress: dict) -> list[str]:
        return [
            str(statement).strip()
            for statement in progress.get("statements", [])
            if str(statement).strip()
        ]

    def failed_validation_count(self, query_results: Iterable[dict]) -> int:
        return sum(1 for item in query_results if item.get("validation", {}).get("status") == "failed")

    def complete_progress(self, progress: dict) -> tuple[list[dict], list[str], int]:
        ordered_results = self.ordered_results(progress)
        ordered_statements = self.valid_rewritten_statements(ordered_results)
        invalid_count = self.failed_validation_count(ordered_results)
        progress["query_results"] = ordered_results
        progress["statements"] = ordered_statements
        progress["completed_query_count"] = len(ordered_statements)
        progress["processed_query_count"] = len(ordered_results)
        return ordered_results, ordered_statements, invalid_count

    def raw_response_text(self, progress: dict) -> str:
        return "\n\n".join(str(item) for item in progress.get("raw_responses", []) if str(item).strip())

    def cache_path(self) -> Path:
        return self._config.resolve_path(self._config.storage.artifact_dir) / "query_rewrite_cache.json"

    def load_cache(self) -> dict:
        path = self.cache_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_cache(self, cache: dict) -> None:
        path = self.cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=True, indent=2), encoding="utf-8")

    def cache_key(self, source_statement: str, target_tables: list[dict]) -> str:
        payload = {
            "original_database": self._config.postgres.original_database,
            "new_database": self._config.postgres.new_database,
            "schemas": list(self._config.postgres.schemas),
            "source_statement": self.normalize_sql(source_statement),
            "target_tables": target_tables,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()

    def normalize_sql(self, statement: str) -> str:
        return re.sub(r"\s+", " ", statement).strip()
