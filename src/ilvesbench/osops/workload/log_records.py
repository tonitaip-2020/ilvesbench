from __future__ import annotations

import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Protocol


MESSAGE_SQL_RE = re.compile(
    r"(?:duration:\s+[0-9.]+\s+ms\s+)?(?:execute\s+[^:]+:\s+|statement:\s+)(?P<sql>.+)$",
    re.IGNORECASE,
)
PID_RE = re.compile(r"\[(?P<pid>\d+)\]")
DURATION_RE = re.compile(r"duration:\s+(?P<duration>[0-9.]+)\s+ms", re.IGNORECASE)

CSVLOG_FIELDS = [
    "log_time",
    "user_name",
    "database_name",
    "process_id",
    "connection_from",
    "session_id",
    "session_line_num",
    "command_tag",
    "session_start_time",
    "virtual_transaction_id",
    "transaction_id",
    "error_severity",
    "sql_state_code",
    "message",
    "detail",
    "hint",
    "internal_query",
    "internal_query_pos",
    "context",
    "query",
    "query_pos",
    "location",
    "application_name",
    "backend_type",
    "leader_pid",
    "query_id",
]


@dataclass(slots=True)
class LogQueryObservation:
    timestamp: str
    database_name: str
    user_name: str
    application_name: str
    client_addr: str
    backend_pid: str
    raw_sql: str
    duration_ms: float | None
    source_file: str
    source_line: int
    log_format: str


class LogRecordParser(Protocol):
    def parse_line(self, line: str, *, source_file: str, source_line: int) -> LogQueryObservation | None:
        ...


class JsonLogRecordParser:
    log_format = "jsonlog"

    def parse_line(self, line: str, *, source_file: str, source_line: int) -> LogQueryObservation | None:
        stripped = line.strip()
        if not stripped.startswith("{"):
            return None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        raw_sql = _extract_sql(
            str(payload.get("query") or payload.get("statement") or ""),
            str(payload.get("message") or ""),
        )
        if not raw_sql:
            return None
        return LogQueryObservation(
            timestamp=str(payload.get("timestamp") or payload.get("log_time") or ""),
            database_name=_database_name(payload.get("dbname") or payload.get("database_name")),
            user_name=str(payload.get("user") or payload.get("user_name") or ""),
            application_name=str(payload.get("application_name") or ""),
            client_addr=str(payload.get("remote_host") or payload.get("client_addr") or ""),
            backend_pid=str(payload.get("pid") or payload.get("process_id") or ""),
            raw_sql=raw_sql,
            duration_ms=_duration_ms(payload.get("duration") or payload.get("duration_ms") or payload.get("message")),
            source_file=source_file,
            source_line=source_line,
            log_format=self.log_format,
        )


class CsvLogRecordParser:
    log_format = "csvlog"

    def parse_line(self, line: str, *, source_file: str, source_line: int) -> LogQueryObservation | None:
        if "," not in line:
            return None
        try:
            row = next(csv.reader([line]))
        except csv.Error:
            return None
        if len(row) < 14:
            return None
        payload = dict(zip(CSVLOG_FIELDS, row))
        raw_sql = _extract_sql(payload.get("query", ""), payload.get("message", ""))
        if not raw_sql:
            return None
        return LogQueryObservation(
            timestamp=payload.get("log_time", ""),
            database_name=_database_name(payload.get("database_name")),
            user_name=payload.get("user_name", ""),
            application_name=payload.get("application_name", ""),
            client_addr=payload.get("connection_from", ""),
            backend_pid=payload.get("process_id", ""),
            raw_sql=raw_sql,
            duration_ms=_duration_ms(payload.get("message", "")),
            source_file=source_file,
            source_line=source_line,
            log_format=self.log_format,
        )


class PlainTextLogRecordParser:
    log_format = "stderr"

    def parse_line(self, line: str, *, source_file: str, source_line: int) -> LogQueryObservation | None:
        raw_sql = _extract_sql("", line)
        if not raw_sql:
            return None
        return LogQueryObservation(
            timestamp="",
            database_name="unknown",
            user_name="",
            application_name="",
            client_addr="",
            backend_pid=_pid(line),
            raw_sql=raw_sql,
            duration_ms=_duration_ms(line),
            source_file=source_file,
            source_line=source_line,
            log_format=self.log_format,
        )


class CompositeLogRecordParser:
    def __init__(self, parsers: list[LogRecordParser] | None = None) -> None:
        self._parsers = parsers or [
            JsonLogRecordParser(),
            CsvLogRecordParser(),
            PlainTextLogRecordParser(),
        ]

    def parse_line(self, line: str, *, source_file: str, source_line: int) -> LogQueryObservation | None:
        for parser in self._parsers:
            observation = parser.parse_line(line, source_file=source_file, source_line=source_line)
            if observation is not None and _is_workload_sql(observation.raw_sql):
                return observation
        return None


def _extract_sql(query_field: str, message: str) -> str:
    query = str(query_field or "").strip()
    if query and query != "<not logged>":
        return query.rstrip(";") + ";"
    match = MESSAGE_SQL_RE.search(str(message or "").strip())
    if not match:
        return ""
    return match.group("sql").strip().rstrip(";") + ";"


def _is_workload_sql(sql: str) -> bool:
    keyword = sql.strip().rstrip(";").split(None, 1)[0].upper() if sql.strip() else ""
    return keyword in {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE"}


def _database_name(value) -> str:
    name = str(value or "").strip()
    return name or "unknown"


def _duration_ms(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    match = DURATION_RE.search(str(value))
    if not match:
        return None
    return float(match.group("duration"))


def _pid(line: str) -> str:
    match = PID_RE.search(line)
    return match.group("pid") if match else ""


def source_name(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())
