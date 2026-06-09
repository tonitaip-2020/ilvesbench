from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Iterator

from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import LLMResult


_CURRENT_RUN_ID: ContextVar[str | None] = ContextVar("ilvesbench_run_id", default=None)
_CURRENT_STEP: ContextVar[str | None] = ContextVar("ilvesbench_step", default=None)
_CURRENT_COMPONENT: ContextVar[str | None] = ContextVar("ilvesbench_component", default=None)


SEVERITY_COLORS = {
    "debug": "\033[90m",
    "info": "\033[36m",
    "success": "\033[32m",
    "warning": "\033[33m",
    "error": "\033[31m",
    "llm": "\033[35m",
}
RESET = "\033[0m"


class DiagnosticLogger:
    def __init__(self, artifact_dir: str | Path, *, echo_to_terminal: bool | None = None) -> None:
        self._artifact_dir = Path(artifact_dir).expanduser().resolve()
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._echo_to_terminal = sys.stderr.isatty() if echo_to_terminal is None else echo_to_terminal
        self._lock = threading.Lock()
        self._counter = 0

    def activate(
        self,
        run_id: str | None = None,
        *,
        step: str | None = None,
        component: str | None = None,
    ) -> tuple[Any, Any, Any]:
        return (
            _CURRENT_RUN_ID.set(run_id),
            _CURRENT_STEP.set(step),
            _CURRENT_COMPONENT.set(component),
        )

    def reset(self, tokens: tuple[Any, Any, Any]) -> None:
        run_token, step_token, component_token = tokens
        _CURRENT_COMPONENT.reset(component_token)
        _CURRENT_STEP.reset(step_token)
        _CURRENT_RUN_ID.reset(run_token)

    @contextmanager
    def scoped(
        self,
        *,
        run_id: str | None = None,
        step: str | None = None,
        component: str | None = None,
    ) -> Iterator[None]:
        tokens = self.activate(
            run_id if run_id is not None else _CURRENT_RUN_ID.get(),
            step=step if step is not None else _CURRENT_STEP.get(),
            component=component if component is not None else _CURRENT_COMPONENT.get(),
        )
        try:
            yield
        finally:
            self.reset(tokens)

    def event(
        self,
        message: str,
        *,
        severity: str = "info",
        component: str | None = None,
        step: str | None = None,
        run_id: str | None = None,
        details: dict[str, Any] | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": run_id if run_id is not None else _CURRENT_RUN_ID.get(),
            "step": step if step is not None else _CURRENT_STEP.get(),
            "component": component if component is not None else _CURRENT_COMPONENT.get() or "system",
            "severity": severity,
            "message": message,
            "details": redact(details or {}),
            "artifacts": artifacts or {},
        }
        with self._lock:
            self._counter += 1
            event["sequence"] = self._counter
            self._write_event(event)
        if self._echo_to_terminal:
            self._echo(event)
        return event

    def write_text(
        self,
        run_id: str | None,
        category: str,
        name: str,
        content: str,
        *,
        suffix: str = ".txt",
    ) -> str:
        path = self._log_dir(run_id) / category / f"{safe_filename(name)}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)

    def write_json(
        self,
        run_id: str | None,
        category: str,
        name: str,
        payload: Any,
        *,
        suffix: str = ".json",
    ) -> str:
        path = self._log_dir(run_id) / category / f"{safe_filename(name)}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(redact(payload), ensure_ascii=True, indent=2, default=str), encoding="utf-8")
        return str(path)

    def read_run_logs(self, run_id: str, *, limit: int = 300) -> dict[str, Any]:
        log_dir = self._log_dir(run_id)
        events_path = log_dir / "events.jsonl"
        human_path = log_dir / "events.log"
        events: list[dict[str, Any]] = []
        if events_path.exists():
            lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-limit:]:
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"severity": "warning", "message": line})
        human_log = ""
        if human_path.exists():
            human_lines = human_path.read_text(encoding="utf-8", errors="replace").splitlines()
            human_log = "\n".join(human_lines[-limit:])
        return {
            "status": "ok" if events or human_log else "empty",
            "run_id": run_id,
            "event_count": len(events),
            "events": events,
            "human_log": human_log,
            "paths": {
                "jsonl": str(events_path),
                "human": str(human_path),
                "directory": str(log_dir),
            },
        }

    def _write_event(self, event: dict[str, Any]) -> None:
        log_dir = self._log_dir(event.get("run_id"))
        log_dir.mkdir(parents=True, exist_ok=True)
        json_line = json.dumps(event, ensure_ascii=True, default=str)
        with (log_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json_line + "\n")
        with (log_dir / "events.log").open("a", encoding="utf-8") as handle:
            handle.write(self._format_plain(event) + "\n")

    def _log_dir(self, run_id: str | None) -> Path:
        if run_id:
            return self._artifact_dir / run_id / "logs"
        return self._artifact_dir / "_server" / "logs"

    def _echo(self, event: dict[str, Any]) -> None:
        line = self._format_plain(event)
        if sys.stderr.isatty():
            color = SEVERITY_COLORS.get(str(event.get("severity")), "")
            if color:
                line = f"{color}{line}{RESET}"
        print(line, file=sys.stderr)

    def _format_plain(self, event: dict[str, Any]) -> str:
        timestamp = str(event.get("timestamp", ""))
        severity = str(event.get("severity", "info")).upper().ljust(7)
        run_id = event.get("run_id") or "server"
        component = event.get("component") or "system"
        step = event.get("step") or "-"
        message = event.get("message") or ""
        return f"[{timestamp}] {severity} run={run_id} component={component} step={step} {message}"


class LoggingLLMGateway(LLMGateway):
    def __init__(self, wrapped: LLMGateway, diagnostics: DiagnosticLogger) -> None:
        self._wrapped = wrapped
        self._diagnostics = diagnostics

    def generate(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 256,
    ) -> LLMResult:
        run_id = _CURRENT_RUN_ID.get()
        step = _CURRENT_STEP.get()
        call_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}_{threading.get_ident()}"
        request_payload = {
            "model": model,
            "max_tokens": max_tokens,
            "message_count": len(messages),
            "messages": messages,
        }
        request_path = self._diagnostics.write_json(run_id, "llm", f"{call_id}_request", request_payload)
        started = time.monotonic()
        self._diagnostics.event(
            "LLM request sent.",
            severity="llm",
            component="llm",
            step=step,
            details={
                "model": model,
                "max_tokens": max_tokens,
                "message_count": len(messages),
                "request_path": request_path,
            },
            artifacts={"request": request_path},
        )
        try:
            result = self._wrapped.generate(messages, model=model, max_tokens=max_tokens)
        except Exception as exc:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            self._diagnostics.event(
                "LLM request failed.",
                severity="error",
                component="llm",
                step=step,
                details={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "duration_ms": duration_ms,
                    "request_path": request_path,
                },
                artifacts={"request": request_path},
            )
            raise

        duration_ms = round((time.monotonic() - started) * 1000, 2)
        response_path = self._diagnostics.write_text(run_id, "llm", f"{call_id}_response", result.response_text)
        raw_path = self._diagnostics.write_json(run_id, "llm", f"{call_id}_raw_response", result.raw_response)
        self._diagnostics.event(
            "LLM response received.",
            severity="llm",
            component="llm",
            step=step,
            details={
                "backend": result.backend,
                "model": result.model,
                "duration_ms": duration_ms,
                "response_chars": len(result.response_text or ""),
                "request_path": request_path,
                "response_path": response_path,
                "raw_response_path": raw_path,
            },
            artifacts={"request": request_path, "response": response_path, "raw_response": raw_path},
        )
        return result


def safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe[:160] or "artifact"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in ("password", "api_key", "apikey", "secret", "token", "authorization")):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value, flags=re.IGNORECASE)
        text = re.sub(r"(PGPASSWORD=)[^\s]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
        text = re.sub(r"(password\s*=\s*)[^\s;]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
        text = re.sub(r"(postgres(?:ql)?://[^:/@\s]+:)[^@\s]+(@)", r"\1[REDACTED]\2", text, flags=re.IGNORECASE)
        return text
    return value
