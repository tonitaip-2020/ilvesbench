from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path, PureWindowsPath
import threading
from urllib.parse import urlparse

from ilvesbench.agent.orchestrator import PipelineOrchestrator
from ilvesbench.config import IlvesBenchConfig


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class IlvesBenchServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], config_path: str) -> None:
        super().__init__(address, IlvesBenchRequestHandler)
        self.config_path = str(Path(config_path).resolve())
        self.orchestrator = PipelineOrchestrator(self._load_config(config_path))

    def reload_config(
        self,
        config_path: str,
        workload_path: str | None = None,
        original_database: str | None = None,
        new_database: str | None = None,
        schemas: list[str] | str | None = None,
    ) -> None:
        self.config_path = str(Path(config_path).resolve())
        config = self._load_config(config_path)
        if workload_path is not None:
            config.workload.path = workload_path.strip() or None
        if original_database is not None and original_database.strip():
            config.postgres.original_database = original_database.strip()
        if new_database is not None and new_database.strip():
            config.postgres.new_database = new_database.strip()
        if schemas is not None:
            schema_values = schemas.split(",") if isinstance(schemas, str) else schemas
            selected_schemas = [schema.strip() for schema in schema_values if schema.strip()]
            if selected_schemas:
                config.postgres.schemas = selected_schemas
        self.orchestrator = PipelineOrchestrator(config)

    def _load_config(self, config_path: str) -> IlvesBenchConfig:
        return IlvesBenchConfig.from_toml(config_path)


class IlvesBenchRequestHandler(BaseHTTPRequestHandler):
    server: IlvesBenchServer
    server_version = "IlvesBench/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"status": "ok", "config_path": self.server.config_path})
            return
        if parsed.path == "/api/runs":
            self._send_json({"runs": self.server.orchestrator.store.list_runs()})
            return
        if "/artifacts/" in parsed.path and parsed.path.startswith("/api/runs/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 5:
                run_id = parts[2]
                artifact_name = parts[4]
                payload = self.server.orchestrator.store.get_run(run_id)
                if payload is None:
                    self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                artifact_path = payload.get("artifacts", {}).get(artifact_name)
                if not artifact_path:
                    self._send_json({"error": "Artifact not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                target = self._resolve_artifact_path(run_id, artifact_path)
                if not target.exists():
                    self._send_json({"error": "Artifact file is missing."}, status=HTTPStatus.NOT_FOUND)
                    return
                self._send_json(json.loads(target.read_text(encoding="utf-8")))
                return
        if parsed.path.startswith("/api/runs/"):
            run_id = parsed.path.rsplit("/", 1)[-1]
            payload = self.server.orchestrator.store.get_run(run_id)
            if payload is None:
                self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(payload)
            return
        if parsed.path in {"/", "/index.html"}:
            self._serve_static("index.html")
            return
        if parsed.path.startswith("/static/"):
            self._serve_static(parsed.path.removeprefix("/static/"))
            return
        self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_json_body()
        if parsed.path == "/api/runs":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
            )
            record = self.server.orchestrator.create_mvp_record()
            thread = threading.Thread(
                target=self.server.orchestrator.execute_mvp_collection,
                args=(record,),
                daemon=True,
            )
            thread.start()
            self._send_json(
                {
                    "status": "accepted",
                    "config_path": self.server.config_path,
                    "run_id": record.run_id,
                },
                status=HTTPStatus.ACCEPTED,
            )
            return
        if parsed.path == "/api/runs/actions/regenerate-rewrite":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
            )
            record = self.server.orchestrator.create_state_resume_record()
            thread = threading.Thread(
                target=self.server.orchestrator.execute_query_migration_from_current_state,
                args=(record,),
                daemon=True,
            )
            thread.start()
            self._send_json(
                {"status": "accepted", "run_id": record.run_id, "action": "regenerate-rewrite"},
                status=HTTPStatus.ACCEPTED,
            )
            return
        if parsed.path.endswith("/actions/create-schema"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_create_target_schema(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_create_target_schema,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "create-schema"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path.endswith("/actions/reset-target-db"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_reset_target_db(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_reset_target_db,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "reset-target-db"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path.endswith("/actions/truncate-target-data"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_truncate_target_data(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_truncate_target_data,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "truncate-target-data"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path.endswith("/actions/repair-schema"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_repair_target_schema(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_repair_target_schema,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "repair-schema"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path.endswith("/actions/normalization-review"):
            run_id = parsed.path.split("/")[-3]
            record = self.server.orchestrator.apply_normalization_review(
                run_id,
                str(body.get("candidate_id", "")),
                str(body.get("decision", "")),
            )
            self._send_json(
                {
                    "status": "completed",
                    "run_id": record.run_id,
                    "action": "normalization-review",
                    "run_status": record.status,
                }
            )
            return
        if parsed.path.endswith("/actions/migrate-data"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_migrate_data(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_migrate_data,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "migrate-data"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path.endswith("/actions/regenerate-rewrite"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_regenerate_rewrite_queries(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_regenerate_rewrite_queries,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "regenerate-rewrite"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path.endswith("/actions/run-pgbench-original"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_pgbench_original(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_pgbench_original,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "run-pgbench-original"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path.endswith("/actions/run-pgbench-new"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_pgbench_new(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_pgbench_new,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "run-pgbench-new"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path.endswith("/actions/create-secondary-indexes"):
            run_id = parsed.path.split("/")[-3]
            self.server.orchestrator.begin_create_secondary_indexes(run_id)
            thread = threading.Thread(
                target=self.server.orchestrator.execute_create_secondary_indexes,
                args=(run_id,),
                daemon=True,
            )
            thread.start()
            self._send_json({"status": "accepted", "run_id": run_id, "action": "create-secondary-indexes"}, status=HTTPStatus.ACCEPTED)
            return
        if parsed.path == "/api/llm/test":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(config_path)
            result = self.server.orchestrator.test_llm()
            self._send_json(result)
            return
        if parsed.path == "/api/postgres/discover":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
            )
            result = self.server.orchestrator.discover_postgres_databases()
            self._send_json(result)
            return
        if parsed.path == "/api/postgres/status":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
            )
            result = self.server.orchestrator.check_postgres_connection()
            self._send_json(result)
            return
        if parsed.path == "/api/postgres/profile":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
            )
            profiles = self.server.orchestrator.profile_databases()
            self._send_json({"status": "ok", "profiles": profiles})
            return
        if parsed.path == "/api/workload/status":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
            )
            self._send_json(self.server.orchestrator.workload_source_status())
            return
        self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return

    def _serve_static(self, relative_path: str) -> None:
        target = (STATIC_DIR / relative_path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send_json({"error": "Static asset not found."}, status=HTTPStatus.NOT_FOUND)
            return
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _resolve_artifact_path(self, run_id: str, artifact_path: str) -> Path:
        target = Path(artifact_path)
        if target.exists():
            return target
        filename = PureWindowsPath(artifact_path).name
        if not filename or filename == artifact_path:
            filename = target.name
        relocated = self.server.orchestrator._config.resolve_path(
            self.server.orchestrator._config.storage.artifact_dir
        ) / run_id / filename
        return relocated if relocated.exists() else target

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str, port: int, config_path: str) -> IlvesBenchServer:
    return IlvesBenchServer((host, port), config_path)
