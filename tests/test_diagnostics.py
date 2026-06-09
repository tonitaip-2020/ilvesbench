from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.diagnostics import DiagnosticLogger, LoggingLLMGateway, redact
from ilvesbench.llm.gateway import LLMGateway
from ilvesbench.models import LLMResult


class StaticGateway(LLMGateway):
    def generate(self, messages, model=None, max_tokens=256) -> LLMResult:
        return LLMResult(
            backend="test",
            model=model or "test-model",
            response_text="SELECT 1;",
            raw_response={"ok": True},
        )


class DiagnosticsTests(unittest.TestCase):
    def test_redacts_sensitive_values(self) -> None:
        payload = {
            "password": "secret",
            "Authorization": "Bearer abc123",
            "dsn": "postgresql://user:supersecret@localhost/db",
            "safe": "hello",
        }

        redacted = redact(payload)

        self.assertEqual(redacted["password"], "[REDACTED]")
        self.assertEqual(redacted["Authorization"], "[REDACTED]")
        self.assertIn("[REDACTED]", redacted["dsn"])
        self.assertEqual(redacted["safe"], "hello")

    def test_logging_llm_gateway_writes_run_logs_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = DiagnosticLogger(tmp, echo_to_terminal=False)
            gateway = LoggingLLMGateway(StaticGateway(), logger)
            tokens = logger.activate("run-test", step="rewrite_queries", component="orchestrator")
            try:
                result = gateway.generate([{"role": "user", "content": "rewrite this"}], max_tokens=64)
            finally:
                logger.reset(tokens)

            self.assertEqual(result.response_text, "SELECT 1;")
            log_payload = logger.read_run_logs("run-test")
            self.assertEqual(log_payload["status"], "ok")
            messages = [event["message"] for event in log_payload["events"]]
            self.assertIn("LLM request sent.", messages)
            self.assertIn("LLM response received.", messages)
            request_events = [event for event in log_payload["events"] if event["message"] == "LLM request sent."]
            request_path = Path(request_events[0]["artifacts"]["request"])
            self.assertTrue(request_path.exists())
            request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request_payload["message_count"], 1)


if __name__ == "__main__":
    unittest.main()
