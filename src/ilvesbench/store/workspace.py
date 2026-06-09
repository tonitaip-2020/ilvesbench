from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any


class DatabaseWorkspaceStore:
    """Durable database-pair artifact storage.

    Runs remain useful as execution/audit logs, but generated SQL and workload
    artifacts belong to the selected source/target database pair.
    """

    def __init__(self, artifact_dir: str | Path) -> None:
        self._root = Path(artifact_dir).expanduser().resolve() / "_workspaces"
        self._root.mkdir(parents=True, exist_ok=True)

    def context(
        self,
        *,
        source_database: str,
        target_database: str,
        schemas: list[str],
        workload_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        schema_values = [schema.strip() for schema in schemas if schema.strip()]
        return {
            "workspace_id": self.workspace_id(source_database, target_database, schema_values),
            "source_database": source_database.strip(),
            "target_database": target_database.strip(),
            "schemas": schema_values,
            "workload_source": workload_source or {},
        }

    def workspace_id(self, source_database: str, target_database: str, schemas: list[str]) -> str:
        payload = json.dumps(
            {
                "source_database": source_database.strip(),
                "target_database": target_database.strip(),
                "schemas": [schema.strip() for schema in schemas if schema.strip()],
            },
            sort_keys=True,
        )
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
        return f"{self._slug(source_database)}__{self._slug(target_database)}__{digest}"

    def upsert_artifact(
        self,
        context: dict[str, Any],
        key: str,
        payload: dict[str, Any],
        *,
        sql_text: str | None = None,
        sql_suffix: str = ".sql",
        source_run_id: str | None = None,
    ) -> dict[str, Any]:
        manifest = self.load_manifest(context)
        workspace_dir = self._workspace_dir(context["workspace_id"])
        workspace_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        artifact_payload = dict(payload)
        artifact_payload.setdefault("workspace_id", context["workspace_id"])
        artifact_payload.setdefault("source_database", context["source_database"])
        artifact_payload.setdefault("target_database", context["target_database"])
        artifact_payload.setdefault("schemas", context["schemas"])
        if source_run_id:
            artifact_payload["source_run_id"] = source_run_id
        json_path = workspace_dir / f"{key}.json"
        json_path.write_text(json.dumps(artifact_payload, ensure_ascii=True, indent=2, default=str), encoding="utf-8")
        record = {
            "key": key,
            "json_path": str(json_path),
            "updated_at": now,
            "source_run_id": source_run_id or "",
            "summary": artifact_payload.get("summary", ""),
            "status": artifact_payload.get("status", ""),
        }
        if sql_text is not None:
            sql_path = workspace_dir / f"{key}{sql_suffix}"
            sql_path.write_text(sql_text, encoding="utf-8")
            record["sql_path"] = str(sql_path)
            artifact_payload["sql_path"] = str(sql_path)
            json_path.write_text(json.dumps(artifact_payload, ensure_ascii=True, indent=2, default=str), encoding="utf-8")
        manifest["artifacts"][key] = record
        manifest["updated_at"] = now
        manifest["workload_source"] = context.get("workload_source", {})
        self._write_manifest(manifest)
        return record

    def save_sql(
        self,
        context: dict[str, Any],
        key: str,
        sql_text: str,
        *,
        source: str = "user_edited",
    ) -> dict[str, Any]:
        manifest = self.load_manifest(context)
        workspace_dir = self._workspace_dir(context["workspace_id"])
        workspace_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        sql_path = workspace_dir / f"{key}.sql"
        sql_path.write_text(sql_text, encoding="utf-8")
        payload = self._read_json(manifest.get("artifacts", {}).get(key, {}).get("json_path"))
        payload.update(
            {
                "workspace_id": context["workspace_id"],
                "source_database": context["source_database"],
                "target_database": context["target_database"],
                "schemas": context["schemas"],
                "source": source,
                "sql_path": str(sql_path),
                "sql_text": sql_text,
                "updated_at": now,
            }
        )
        json_path = workspace_dir / f"{key}.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, default=str), encoding="utf-8")
        manifest["artifacts"][key] = {
            **manifest.get("artifacts", {}).get(key, {}),
            "key": key,
            "json_path": str(json_path),
            "sql_path": str(sql_path),
            "updated_at": now,
            "source": source,
            "summary": f"Saved edited SQL for {key}.",
        }
        manifest["updated_at"] = now
        manifest["workload_source"] = context.get("workload_source", {})
        self._write_manifest(manifest)
        return manifest["artifacts"][key]

    def status(self, context: dict[str, Any]) -> dict[str, Any]:
        manifest = self.load_manifest(context)
        artifacts: dict[str, Any] = {}
        for key, record in manifest.get("artifacts", {}).items():
            payload = self._read_json(record.get("json_path"))
            sql_text = self._read_text(record.get("sql_path"))
            if sql_text and "sql_text" not in payload:
                payload["sql_text"] = sql_text
            if record.get("sql_path"):
                payload.setdefault("sql_path", record["sql_path"])
            artifacts[key] = payload
        return {
            **manifest,
            "artifacts": artifacts,
            "artifact_records": manifest.get("artifacts", {}),
        }

    def load_manifest(self, context: dict[str, Any]) -> dict[str, Any]:
        path = self._manifest_path(context["workspace_id"])
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        now = datetime.now(UTC).isoformat()
        return {
            "workspace_id": context["workspace_id"],
            "source_database": context["source_database"],
            "target_database": context["target_database"],
            "schemas": context["schemas"],
            "workload_source": context.get("workload_source", {}),
            "created_at": now,
            "updated_at": now,
            "artifacts": {},
        }

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(manifest["workspace_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2, default=str), encoding="utf-8")

    def _manifest_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "workspace.json"

    def _workspace_dir(self, workspace_id: str) -> Path:
        return self._root / self._slug(workspace_id)

    def _read_json(self, path_value: str | None) -> dict[str, Any]:
        if not path_value:
            return {}
        path = Path(path_value)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_text(self, path_value: str | None) -> str:
        if not path_value:
            return ""
        path = Path(path_value)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def _slug(self, value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())[:120] or "default"
