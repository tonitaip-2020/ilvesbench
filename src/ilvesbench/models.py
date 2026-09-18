from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def to_dict(instance: Any) -> dict[str, Any]:
    return asdict(instance)


def benchmark_run_record_from_dict(payload: dict[str, Any]) -> "BenchmarkRunRecord":
    return BenchmarkRunRecord(
        run_id=str(payload["run_id"]),
        created_at=str(payload["created_at"]),
        updated_at=str(payload["updated_at"]),
        status=str(payload["status"]),
        config_path=str(payload["config_path"]),
        summary=dict(payload.get("summary", {})),
        steps=[step_result_from_dict(step) for step in payload.get("steps", [])],
        artifacts=dict(payload.get("artifacts", {})),
    )


def step_result_from_dict(payload: dict[str, Any]) -> "StepResult":
    return StepResult(
        name=str(payload["name"]),
        title=str(payload["title"]),
        status=str(payload["status"]),
        requires_approval=bool(payload.get("requires_approval", False)),
        planned_only=bool(payload.get("planned_only", False)),
        details=dict(payload.get("details", {})),
        error=payload.get("error"),
    )


@dataclass(slots=True)
class ColumnMetadata:
    name: str
    data_type: str
    is_nullable: bool
    default: str | None = None


@dataclass(slots=True)
class IndexMetadata:
    name: str
    definition: str
    is_unique: bool = False


@dataclass(slots=True)
class ForeignKeyMetadata:
    name: str
    columns: list[str]
    referenced_table: str
    referenced_columns: list[str]


@dataclass(slots=True)
class UniqueConstraintMetadata:
    name: str
    columns: list[str]


@dataclass(slots=True)
class TableMetadata:
    schema: str
    name: str
    columns: list[ColumnMetadata] = field(default_factory=list)
    indexes: list[IndexMetadata] = field(default_factory=list)
    foreign_keys: list[ForeignKeyMetadata] = field(default_factory=list)
    unique_constraints: list[UniqueConstraintMetadata] = field(default_factory=list)


@dataclass(slots=True)
class SchemaSnapshot:
    database: str
    collected_at: str
    tables: list[TableMetadata]
    database_size_bytes: int | None = None


@dataclass(slots=True)
class QueryObservation:
    fingerprint: str
    sample_sql: str
    count: int
    total_duration_ms: float = 0.0
    proportion: float = 0.0
    database_name: str = ""
    normalized_sql: str = ""
    raw_sql: str = ""
    parameters: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class TransactionObservation:
    pid: str
    statement_count: int
    statements: list[str]


@dataclass(slots=True)
class LogSummary:
    path: str
    lines_processed: int
    statements_detected: int
    transactions_detected: int
    multi_statement_transactions: int
    top_queries: list[QueryObservation]
    sampled_transactions: list[TransactionObservation]
    source_kind: str = "postgres_log"
    workload_outputs: dict[str, dict] = field(default_factory=dict)
    skipped_statements: list[dict] = field(default_factory=list)
    skipped_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HardwareSnapshot:
    collected_at: str
    scope: str
    platform: str
    cpu_count: int | None
    architecture: str
    memory_total_bytes: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    container_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMResult:
    backend: str
    model: str
    response_text: str
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BenchmarkMetrics:
    database: str
    status: str
    command: list[str]
    duration_seconds: int
    clients: int
    jobs: int
    transactions: int | None
    throughput_tps: float | None = None
    average_latency_ms: float | None = None
    progress_samples: list[dict[str, float]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class StepResult:
    name: str
    title: str
    status: str
    requires_approval: bool = False
    planned_only: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class BenchmarkRunRecord:
    run_id: str
    created_at: str
    updated_at: str
    status: str
    config_path: str
    summary: dict[str, Any] = field(default_factory=dict)
    steps: list[StepResult] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
