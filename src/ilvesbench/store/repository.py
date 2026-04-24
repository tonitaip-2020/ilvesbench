from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from ilvesbench.models import BenchmarkRunRecord, benchmark_run_record_from_dict, to_dict


class RunRepository:
    def __init__(self, sqlite_path: str | Path, artifact_dir: str | Path) -> None:
        self._sqlite_path = Path(sqlite_path).expanduser().resolve()
        self._artifact_dir = Path(artifact_dir).expanduser().resolve()
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def upsert_run(self, record: BenchmarkRunRecord) -> None:
        payload = json.dumps(to_dict(record), ensure_ascii=True, indent=2)
        now = datetime.now(UTC).isoformat()
        with closing(sqlite3.connect(self._sqlite_path)) as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, created_at, updated_at, status, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    status = excluded.status,
                    payload_json = excluded.payload_json
                """,
                (record.run_id, record.created_at, now, record.status, payload),
            )
            conn.commit()

    def list_runs(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self._sqlite_path)) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM runs ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self._sqlite_path)) as conn:
            row = conn.execute("SELECT payload_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_run_record(self, run_id: str) -> BenchmarkRunRecord | None:
        payload = self.get_run(run_id)
        return benchmark_run_record_from_dict(payload) if payload else None

    def write_artifact(self, run_id: str, name: str, payload: Any) -> str:
        run_dir = self._artifact_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        return str(path)

    def write_text_artifact(self, run_id: str, name: str, content: str, *, suffix: str = ".txt") -> str:
        run_dir = self._artifact_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{name}{suffix}"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def _initialize(self) -> None:
        with closing(sqlite3.connect(self._sqlite_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.commit()
