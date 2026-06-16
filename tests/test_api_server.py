from http import HTTPStatus
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.api.server import IlvesBenchRequestHandler


class FakeRecord:
    run_id = "run-test"
    status = "completed"


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_mvp_record(self) -> FakeRecord:
        self.calls.append(("create_mvp_record",))
        return FakeRecord()

    def execute_mvp_collection(self, record: FakeRecord) -> None:
        self.calls.append(("execute_mvp_collection", record.run_id))

    def create_workspace_action_record(self) -> FakeRecord:
        self.calls.append(("create_workspace_action_record",))
        return FakeRecord()

    def begin_create_target_schema(self, run_id: str) -> None:
        self.calls.append(("begin_create_target_schema", run_id))

    def execute_create_target_schema(self, run_id: str) -> None:
        self.calls.append(("execute_create_target_schema", run_id))

    def execute_query_rewrite_from_current_state(self, record: FakeRecord) -> None:
        self.calls.append(("execute_query_rewrite_from_current_state", record.run_id))

    def begin_pgbench_original(self, run_id: str) -> None:
        self.calls.append(("begin_pgbench_original", run_id))

    def execute_pgbench_original(self, run_id: str) -> None:
        self.calls.append(("execute_pgbench_original", run_id))

    def apply_normalization_review(self, run_id: str, candidate_id: str, decision: str) -> FakeRecord:
        self.calls.append(("apply_normalization_review", run_id, candidate_id, decision))
        return FakeRecord()


class FakeServer:
    config_path = "/tmp/ilvesbench.toml"

    def __init__(self) -> None:
        self.orchestrator = FakeOrchestrator()
        self.reloads: list[tuple] = []

    def reload_config(self, config_path, **kwargs) -> None:
        self.reloads.append((config_path, kwargs))


def fake_handler() -> tuple[IlvesBenchRequestHandler, FakeServer, list, list]:
    handler = object.__new__(IlvesBenchRequestHandler)
    server = FakeServer()
    sent: list[tuple] = []
    started: list[tuple] = []
    handler.server = server
    handler._send_json = lambda payload, status=HTTPStatus.OK: sent.append((payload, status))
    handler._start_background = lambda target, *args: started.append((target.__name__, args))
    return handler, server, sent, started


class ApiActionDispatchTests(unittest.TestCase):
    def test_workspace_action_uses_registered_begin_execute_pair(self) -> None:
        handler, server, sent, started = fake_handler()

        handler._handle_workspace_action(
            "create-schema",
            {"config_path": "config.toml", "original_database": "source_db"},
        )

        self.assertEqual(server.reloads[0][0], "config.toml")
        self.assertEqual(server.reloads[0][1]["original_database"], "source_db")
        self.assertIn(("create_workspace_action_record",), server.orchestrator.calls)
        self.assertIn(("begin_create_target_schema", "run-test"), server.orchestrator.calls)
        self.assertEqual(started, [("execute_create_target_schema", ("run-test",))])
        self.assertEqual(sent, [({"status": "accepted", "run_id": "run-test", "action": "create-schema"}, HTTPStatus.ACCEPTED)])

    def test_workspace_regenerate_rewrite_starts_record_based_executor(self) -> None:
        handler, server, sent, started = fake_handler()

        handler._handle_workspace_action("regenerate-rewrite", {})

        self.assertIn(("create_workspace_action_record",), server.orchestrator.calls)
        self.assertNotIn(("begin_regenerate_rewrite_queries", "run-test"), server.orchestrator.calls)
        self.assertEqual(started[0][0], "execute_query_rewrite_from_current_state")
        self.assertIsInstance(started[0][1][0], FakeRecord)
        self.assertEqual(sent[0][0]["action"], "regenerate-rewrite")

    def test_run_action_can_reload_config_before_starting(self) -> None:
        handler, server, sent, started = fake_handler()

        handler._handle_run_action("run-123", "run-pgbench-original", {"config_path": "bench.toml"})

        self.assertEqual(server.reloads[0][0], "bench.toml")
        self.assertIn(("begin_pgbench_original", "run-123"), server.orchestrator.calls)
        self.assertEqual(started, [("execute_pgbench_original", ("run-123",))])
        self.assertEqual(sent[0], ({"status": "accepted", "run_id": "run-123", "action": "run-pgbench-original"}, HTTPStatus.ACCEPTED))

    def test_review_action_stays_synchronous(self) -> None:
        handler, server, sent, started = fake_handler()

        handler._handle_review_action(
            "run-123",
            "normalization-review",
            {"candidate_id": "candidate-1", "decision": "approved"},
        )

        self.assertEqual(started, [])
        self.assertIn(
            ("apply_normalization_review", "run-123", "candidate-1", "approved"),
            server.orchestrator.calls,
        )
        self.assertEqual(sent[0][0]["status"], "completed")
        self.assertEqual(sent[0][0]["action"], "normalization-review")

    def test_unknown_workspace_action_returns_not_found(self) -> None:
        handler, _server, sent, started = fake_handler()

        handler._handle_workspace_action("missing-action", {})

        self.assertEqual(started, [])
        self.assertEqual(sent, [({"error": "Unknown workspace action."}, HTTPStatus.NOT_FOUND)])

    def test_action_path_parsers_are_exact(self) -> None:
        handler, _server, _sent, _started = fake_handler()

        self.assertEqual(handler._workspace_action_name("/api/runs/actions/create-schema"), "create-schema")
        self.assertEqual(handler._run_action("/api/runs/run-1/actions/create-schema"), ("run-1", "create-schema"))
        self.assertIsNone(handler._workspace_action_name("/api/runs/actions/create-schema/extra"))
        self.assertIsNone(handler._run_action("/api/runs/run-1/create-schema"))


if __name__ == "__main__":
    unittest.main()
