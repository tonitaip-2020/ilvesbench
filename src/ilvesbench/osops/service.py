from __future__ import annotations

from pathlib import Path

from ilvesbench.config import IlvesBenchConfig, PgBenchConfig, PostgresConfig
from ilvesbench.models import LogSummary, to_dict
from ilvesbench.osops.hardware import HardwareInspector
from ilvesbench.osops.logs import PostgresLogParser
from ilvesbench.osops.workload_files import WorkloadFileParser


class OSOpsService:
    """Operating-system boundary for IlvesBench.

    OSOps owns filesystem and host/container inspection: PostgreSQL logs,
    workload files, hardware snapshots, and future postgresql.conf access.
    """

    def __init__(
        self,
        config: IlvesBenchConfig,
        *,
        hardware: HardwareInspector | None = None,
        logs: PostgresLogParser | None = None,
        workload_files: WorkloadFileParser | None = None,
    ) -> None:
        self._config = config
        self.hardware = hardware or HardwareInspector(config.docker)
        self.logs = logs or PostgresLogParser()
        self.workload_files = workload_files or WorkloadFileParser()

    def collect_hardware(self) -> object:
        return self.hardware.collect(self._config.resolve_path("."))

    def resolve_workload_path(self) -> Path | None:
        configured = (self._config.workload.path or "").strip()
        candidates: list[Path] = []
        if configured:
            candidates.append(self._config.resolve_path(configured))
        candidates.append(self._config.resolve_path("data/workload.sql"))
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0] if candidates else None

    def workload_source_context(self) -> dict:
        workload_input = (self._config.workload.path or "").strip()
        workload_path = self.resolve_workload_path()
        log_path = self._config.resolve_path(self._config.logs.path)
        return {
            "configured_workload_path": workload_input or "data/workload.sql",
            "configured_workload_path_was_blank": not workload_input,
            "resolved_workload_path": str(workload_path) if workload_path is not None else "",
            "workload_file_exists": workload_path is not None and workload_path.exists(),
            "log_path": str(log_path),
            "log_file_exists": log_path.exists(),
        }

    def workload_source_status(self) -> dict:
        context = self.workload_source_context()
        if context["log_file_exists"]:
            selected_kind = "postgresql_log"
            selected_label = "PostgreSQL query log"
            selected_path = context["log_path"]
        elif context["workload_file_exists"]:
            selected_kind = "workload_file"
            selected_label = "SQL workload file"
            selected_path = context["resolved_workload_path"]
        else:
            selected_kind = "missing"
            selected_label = "No query source found"
            selected_path = ""
        return {
            "status": "ok" if selected_kind != "missing" else "missing",
            "selected_kind": selected_kind,
            "selected_label": selected_label,
            "selected_path": selected_path,
            **context,
            "path_resolution": "Relative workload paths resolve from the selected config file directory.",
        }

    def load_workload_source(
        self,
        *,
        output_dir: str | Path | None = None,
        postgres: PostgresConfig | None = None,
        pgbench: PgBenchConfig | None = None,
    ) -> LogSummary | None:
        log_path = self._config.resolve_path(self._config.logs.path)
        workload_path = self.resolve_workload_path()
        if log_path.exists():
            return self.logs.parse(
                log_path,
                max_lines=self._config.logs.max_lines,
                output_dir=output_dir,
                postgres=postgres or self._config.postgres,
                pgbench=pgbench or self._config.pgbench,
            )
        if workload_path is not None and workload_path.exists():
            return self.workload_files.parse(workload_path)
        return None

    def workload_source_preview(self) -> dict:
        log_path = self._config.resolve_path(self._config.logs.path)
        workload_path = self.resolve_workload_path()
        log_summary = self.load_workload_source()
        if log_summary is None:
            return {
                "status": "missing",
                "summary": "No PostgreSQL log file or workload SQL file was found.",
                "checked_log_path": str(log_path),
                "checked_workload_path": str(workload_path) if workload_path else "",
            }
        staged_query_text = "\n\n".join(
            (
                f"-- observed query {index} | count {query.count} | proportion {query.proportion * 100:.3f}%\n"
                f"{query.sample_sql.strip().rstrip(';')};"
            )
            for index, query in enumerate(log_summary.top_queries, start=1)
            if query.sample_sql.strip()
        )
        return {
            "status": "ok",
            "source_kind": log_summary.source_kind,
            "source_path": log_summary.path,
            "statements_detected": log_summary.statements_detected,
            "transactions_detected": log_summary.transactions_detected,
            "top_queries": [to_dict(query) for query in log_summary.top_queries],
            "sampled_transactions": [to_dict(transaction) for transaction in log_summary.sampled_transactions],
            "staged_query_text": staged_query_text,
        }

    def postgresql_conf_status(self) -> dict:
        path = self._postgresql_conf_path()
        if path is None:
            return {
                "status": "missing",
                "summary": "No postgresql.conf path is configured.",
                "path": "",
                "exists": False,
                "editable": False,
                "content": "",
            }
        exists = path.exists()
        content = path.read_text(encoding="utf-8", errors="replace") if exists else ""
        return {
            "status": "ok" if exists else "missing",
            "summary": "postgresql.conf loaded." if exists else "Configured postgresql.conf path does not exist yet.",
            "path": str(path),
            "exists": exists,
            "editable": exists and path.is_file(),
            "content": content,
        }

    def save_postgresql_conf(self, content: str) -> dict:
        path = self._postgresql_conf_path()
        if path is None:
            raise ValueError("No postgresql.conf path is configured.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "status": "ok",
            "summary": "postgresql.conf saved. Restart or reload PostgreSQL for changes to take effect.",
            "path": str(path),
            "exists": True,
            "editable": True,
            "content": content,
        }

    def _postgresql_conf_path(self) -> Path | None:
        configured = (self._config.postgresql_conf.path or "").strip()
        if not configured:
            return None
        return self._config.resolve_path(configured)
