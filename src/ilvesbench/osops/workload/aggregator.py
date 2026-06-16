from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .log_records import LogQueryObservation
from .normalizer import PglastSQLNormalizer, SQLNormalizationError, SQLParameter


@dataclass(slots=True)
class WorkloadEntry:
    database_name: str
    fingerprint: str
    normalized_sql: str
    count: int = 0
    ratio: float = 0.0
    example_raw_sqls: list[str] = field(default_factory=list)
    parameters: list[SQLParameter] = field(default_factory=list)
    total_duration_ms: float = 0.0


class WorkloadAggregator:
    def __init__(
        self,
        normalizer: PglastSQLNormalizer | None = None,
        *,
        max_examples: int = 3,
    ) -> None:
        self._normalizer = normalizer or PglastSQLNormalizer()
        self._max_examples = max_examples
        self._entries: dict[str, dict[str, WorkloadEntry]] = {}
        self._skipped: list[dict] = []
        self._observed_count = 0

    @property
    def skipped(self) -> list[dict]:
        return self._skipped

    @property
    def observed_count(self) -> int:
        return self._observed_count

    def observe(self, observation: LogQueryObservation) -> None:
        self._observed_count += 1
        try:
            normalized = self._normalizer.normalize(observation.raw_sql)
        except SQLNormalizationError as exc:
            reason, recommended_action = _skip_reason_and_action(str(exc))
            self._skipped.append(
                {
                    "source_file": observation.source_file,
                    "source_line": observation.source_line,
                    "database_name": observation.database_name,
                    "raw_sql": observation.raw_sql,
                    "reason": reason,
                    "error": str(exc),
                    "recommended_action": recommended_action,
                }
            )
            return

        database_entries = self._entries.setdefault(observation.database_name, {})
        entry = database_entries.get(normalized.fingerprint)
        if entry is None:
            entry = WorkloadEntry(
                database_name=observation.database_name,
                fingerprint=normalized.fingerprint,
                normalized_sql=normalized.normalized_sql,
                parameters=normalized.parameters,
            )
            database_entries[normalized.fingerprint] = entry
        entry.count += 1
        if observation.duration_ms is not None:
            entry.total_duration_ms += observation.duration_ms
        if len(entry.example_raw_sqls) < self._max_examples and observation.raw_sql not in entry.example_raw_sqls:
            entry.example_raw_sqls.append(observation.raw_sql)
        self._merge_parameter_examples(entry.parameters, normalized.parameters)

    def entries_by_database(self) -> dict[str, list[WorkloadEntry]]:
        result: dict[str, list[WorkloadEntry]] = {}
        for database_name, entries in self._entries.items():
            ordered = sorted(entries.values(), key=lambda item: (-item.count, item.fingerprint))
            total = sum(item.count for item in ordered)
            for entry in ordered:
                entry.ratio = round(entry.count / total, 9) if total else 0.0
            result[database_name] = ordered
        return result

    def skipped_summary(self) -> dict:
        if not self._skipped:
            return {"total": 0, "by_reason": {}, "examples": [], "recommended_action": ""}
        by_reason = Counter(str(item.get("reason") or "unknown") for item in self._skipped)
        return {
            "total": len(self._skipped),
            "by_reason": dict(sorted(by_reason.items())),
            "examples": self._skipped[:5],
            "recommended_action": _summary_recommended_action(by_reason),
        }

    def _merge_parameter_examples(self, existing: list[SQLParameter], incoming: Iterable[SQLParameter]) -> None:
        by_position = {item.position: item for item in existing}
        for item in incoming:
            target = by_position.get(item.position)
            if target is None:
                continue
            for example in item.examples:
                if example not in target.examples and len(target.examples) < 10:
                    target.examples.append(example)

    def manifest_entry(self, entry: WorkloadEntry, query_id: str, pgbench_file: str) -> dict:
        return {
            "id": query_id,
            "fingerprint": entry.fingerprint,
            "ratio": entry.ratio,
            "count": entry.count,
            "normalized_sql": entry.normalized_sql,
            "pgbench_file": pgbench_file,
            "example_raw_sqls": entry.example_raw_sqls,
            "parameters": [asdict(parameter) for parameter in entry.parameters],
            "total_duration_ms": round(entry.total_duration_ms, 3),
        }


def _skip_reason_and_action(error: str) -> tuple[str, str]:
    lowered = error.lower()
    if "pglast is required" in lowered:
        return (
            "normalizer_unavailable",
            "Install IlvesBench dependencies in the active virtual environment, then rerun ingestion.",
        )
    if "parser rejected" in lowered:
        return (
            "invalid_sql",
            "Review the skipped SQL text or PostgreSQL logging configuration; the statement could not be parsed.",
        )
    if "empty" in lowered:
        return (
            "empty_statement",
            "Remove empty statements from the workload source.",
        )
    if "expected one sql statement" in lowered:
        return (
            "multi_statement_observation",
            "Configure logging so each observation contains one SQL statement.",
        )
    return (
        "normalization_failed",
        "Review the skipped SQL examples and adjust the workload source before benchmarking.",
    )


def _summary_recommended_action(by_reason: Counter[str]) -> str:
    if by_reason.get("normalizer_unavailable"):
        return "Install project dependencies in the active virtual environment before using PostgreSQL log workloads."
    if by_reason.get("invalid_sql"):
        return "Review skipped SQL examples; invalid statements were ignored while valid statements were preserved."
    return "Review skipped SQL examples; only normalized statements are used for generated pgbench workloads."
