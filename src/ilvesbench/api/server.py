from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class AsyncActionSpec:
    begin_method: str | None
    execute_method: str
    execute_arg: str = "run_id"
    reload_config: bool = False


WORKSPACE_ACTIONS: dict[str, AsyncActionSpec] = {
    "regenerate-rewrite": AsyncActionSpec(
        begin_method=None,
        execute_method="execute_query_rewrite_from_current_state",
        execute_arg="record",
    ),
    "create-schema": AsyncActionSpec("begin_create_target_schema", "execute_create_target_schema"),
    "migrate-data": AsyncActionSpec("begin_migrate_data", "execute_migrate_data"),
    "discover-summary-tables": AsyncActionSpec("begin_discover_summary_tables", "execute_discover_summary_tables"),
    "create-summary-tables": AsyncActionSpec("begin_create_summary_tables", "execute_create_summary_tables"),
    "discover-index-recommendations": AsyncActionSpec(
        "begin_discover_index_recommendations",
        "execute_discover_index_recommendations",
    ),
    "create-secondary-indexes": AsyncActionSpec("begin_create_secondary_indexes", "execute_create_secondary_indexes"),
    "run-pgbench-original": AsyncActionSpec("begin_pgbench_original", "execute_pgbench_original"),
    "run-pgbench-new": AsyncActionSpec("begin_pgbench_new", "execute_pgbench_new"),
    "reset-target-db": AsyncActionSpec("begin_reset_target_db", "execute_reset_target_db"),
    "truncate-target-data": AsyncActionSpec("begin_truncate_target_data", "execute_truncate_target_data"),
}


RUN_ACTIONS: dict[str, AsyncActionSpec] = {
    "create-schema": AsyncActionSpec("begin_create_target_schema", "execute_create_target_schema"),
    "reset-target-db": AsyncActionSpec("begin_reset_target_db", "execute_reset_target_db"),
    "truncate-target-data": AsyncActionSpec("begin_truncate_target_data", "execute_truncate_target_data"),
    "repair-schema": AsyncActionSpec("begin_repair_target_schema", "execute_repair_target_schema"),
    "migrate-data": AsyncActionSpec("begin_migrate_data", "execute_migrate_data"),
    "regenerate-rewrite": AsyncActionSpec(
        "begin_regenerate_rewrite_queries",
        "execute_regenerate_rewrite_queries",
    ),
    "create-summary-tables": AsyncActionSpec("begin_create_summary_tables", "execute_create_summary_tables"),
    "discover-summary-tables": AsyncActionSpec("begin_discover_summary_tables", "execute_discover_summary_tables"),
    "run-pgbench-original": AsyncActionSpec("begin_pgbench_original", "execute_pgbench_original", reload_config=True),
    "run-pgbench-new": AsyncActionSpec("begin_pgbench_new", "execute_pgbench_new", reload_config=True),
    "create-secondary-indexes": AsyncActionSpec("begin_create_secondary_indexes", "execute_create_secondary_indexes"),
    "discover-index-recommendations": AsyncActionSpec(
        "begin_discover_index_recommendations",
        "execute_discover_index_recommendations",
    ),
}


REVIEW_ACTIONS: dict[str, str] = {
    "normalization-review": "apply_normalization_review",
    "summary-table-review": "apply_summary_table_review",
}


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
        pgbench: dict | None = None,
        postgresql_conf_path: str | None = None,
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
        if pgbench:
            self._apply_pgbench_overrides(config, pgbench)
        if postgresql_conf_path is not None:
            config.postgresql_conf.path = postgresql_conf_path.strip() or None
        self.orchestrator = PipelineOrchestrator(config)

    def _load_config(self, config_path: str) -> IlvesBenchConfig:
        return IlvesBenchConfig.from_toml(config_path)

    def _apply_pgbench_overrides(self, config: IlvesBenchConfig, payload: dict) -> None:
        if "enabled" in payload:
            config.pgbench.enabled = bool(payload["enabled"])
        if str(payload.get("command", "")).strip():
            config.pgbench.command = str(payload["command"]).strip()
        for key in ("duration_seconds", "clients", "jobs"):
            if payload.get(key) in ("", None):
                continue
            setattr(config.pgbench, key, max(1, int(payload[key])))
        transactions = payload.get("transactions")
        config.pgbench.transactions = None if transactions in ("", None) else max(1, int(transactions))


class IlvesBenchRequestHandler(BaseHTTPRequestHandler):
    server: IlvesBenchServer
    server_version = "IlvesBench/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"status": "ok", "config_path": self.server.config_path})
            return
        if parsed.path == "/api/runs":
            self.server.orchestrator.recover_stale_running_runs()
            self._send_json({"runs": self.server.orchestrator.store.list_runs()})
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/diagnostics"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 4:
                run_id = parts[2]
                try:
                    self._send_json(self.server.orchestrator.diagnostic_log(run_id))
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
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
            self.server.orchestrator.recover_stale_running_runs()
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
            self._handle_create_run(body)
            return
        workspace_action = self._workspace_action_name(parsed.path)
        if workspace_action:
            self._handle_workspace_action(workspace_action, body)
            return
        run_action = self._run_action(parsed.path)
        if run_action is not None:
            run_id, action = run_action
            if action in REVIEW_ACTIONS:
                self._handle_review_action(run_id, action, body)
            else:
                self._handle_run_action(run_id, action, body)
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
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
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
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
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
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
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
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
            )
            self._send_json(self.server.orchestrator.workload_source_status())
            return
        if parsed.path == "/api/workload/preview":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
            )
            self._send_json(self.server.orchestrator.workload_source_preview())
            return
        if parsed.path == "/api/workspace/status":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
            )
            self._send_json(self.server.orchestrator.workspace_status())
            return
        if parsed.path == "/api/workspace/save-sql":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
            )
            try:
                self._send_json(
                    self.server.orchestrator.save_workspace_sql(
                        str(body.get("artifact_key", "")),
                        str(body.get("content", "")),
                    )
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/pgbench/recommend":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
            )
            self._send_json(self.server.orchestrator.recommend_pgbench_parameters())
            return
        if parsed.path == "/api/postgresql-conf":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
            )
            self._send_json(self.server.orchestrator.postgresql_conf_status())
            return
        if parsed.path == "/api/postgresql-conf/save":
            config_path = body.get("config_path", self.server.config_path)
            self.server.reload_config(
                config_path,
                workload_path=body.get("workload_path"),
                original_database=body.get("original_database"),
                new_database=body.get("new_database"),
                schemas=body.get("schemas"),
                pgbench=body.get("pgbench"),
                postgresql_conf_path=body.get("postgresql_conf_path"),
            )
            self._send_json(self.server.orchestrator.save_postgresql_conf(str(body.get("content", ""))))
            return
        self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def _handle_create_run(self, body: dict) -> None:
        self._reload_config_from_body(body)
        record = self.server.orchestrator.create_mvp_record()
        self._start_background(self.server.orchestrator.execute_mvp_collection, record)
        self._send_json(
            {
                "status": "accepted",
                "config_path": self.server.config_path,
                "run_id": record.run_id,
            },
            status=HTTPStatus.ACCEPTED,
        )

    def _handle_workspace_action(self, action: str, body: dict) -> None:
        spec = WORKSPACE_ACTIONS.get(action)
        if spec is None:
            self._send_json({"error": "Unknown workspace action."}, status=HTTPStatus.NOT_FOUND)
            return
        self._reload_config_from_body(body)
        record = self.server.orchestrator.create_workspace_action_record()
        if spec.begin_method:
            getattr(self.server.orchestrator, spec.begin_method)(record.run_id)
        execute_arg = record if spec.execute_arg == "record" else record.run_id
        self._start_background(getattr(self.server.orchestrator, spec.execute_method), execute_arg)
        self._send_accepted(record.run_id, action)

    def _handle_run_action(self, run_id: str, action: str, body: dict) -> None:
        spec = RUN_ACTIONS.get(action)
        if spec is None:
            self._send_json({"error": "Unknown run action."}, status=HTTPStatus.NOT_FOUND)
            return
        if spec.reload_config:
            self._reload_config_from_body(body)
        if spec.begin_method:
            getattr(self.server.orchestrator, spec.begin_method)(run_id)
        self._start_background(getattr(self.server.orchestrator, spec.execute_method), run_id)
        self._send_accepted(run_id, action)

    def _handle_review_action(self, run_id: str, action: str, body: dict) -> None:
        method_name = REVIEW_ACTIONS[action]
        record = getattr(self.server.orchestrator, method_name)(
            run_id,
            str(body.get("candidate_id", "")),
            str(body.get("decision", "")),
        )
        self._send_json(
            {
                "status": "completed",
                "run_id": record.run_id,
                "action": action,
                "run_status": record.status,
            }
        )

    def _workspace_action_name(self, path: str) -> str | None:
        prefix = "/api/runs/actions/"
        if not path.startswith(prefix):
            return None
        action = path.removeprefix(prefix).strip("/")
        return action if action and "/" not in action else None

    def _run_action(self, path: str) -> tuple[str, str] | None:
        parts = path.strip("/").split("/")
        if len(parts) != 5 or parts[:2] != ["api", "runs"] or parts[3] != "actions":
            return None
        run_id = parts[2].strip()
        action = parts[4].strip()
        if not run_id or not action:
            return None
        return run_id, action

    def _reload_config_from_body(self, body: dict) -> None:
        self.server.reload_config(
            body.get("config_path", self.server.config_path),
            workload_path=body.get("workload_path"),
            original_database=body.get("original_database"),
            new_database=body.get("new_database"),
            schemas=body.get("schemas"),
            pgbench=body.get("pgbench"),
            postgresql_conf_path=body.get("postgresql_conf_path"),
        )

    def _start_background(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()

    def _send_accepted(self, run_id: str, action: str) -> None:
        self._send_json(
            {"status": "accepted", "run_id": run_id, "action": action},
            status=HTTPStatus.ACCEPTED,
        )

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
