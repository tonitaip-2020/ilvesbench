from __future__ import annotations

import argparse
import json
import threading
import webbrowser

from ilvesbench.agent.orchestrator import PipelineOrchestrator
from ilvesbench.api.server import create_server
from ilvesbench.config import IlvesBenchConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IlvesBench PoC")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the web UI server.")
    serve_parser.add_argument("--config", default="ilvesbench.example.toml")
    serve_parser.add_argument("--no-browser", action="store_true")

    run_parser = subparsers.add_parser("run", help="Run one MVP collection pass.")
    run_parser.add_argument("--config", default="ilvesbench.example.toml")

    llm_parser = subparsers.add_parser("test-llm", help="Test the LLM gateway.")
    llm_parser.add_argument("--config", default="ilvesbench.example.toml")

    args = parser.parse_args(argv)

    if args.command == "serve":
        config = IlvesBenchConfig.from_toml(args.config)
        server = create_server(config.web.host, config.web.port, args.config)
        url = f"http://127.0.0.1:{config.web.port}"
        if not args.no_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        print(f"IlvesBench running at http://{config.web.host}:{config.web.port}")
        print("Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping server...")
        finally:
            server.server_close()
        return 0

    config = IlvesBenchConfig.from_toml(args.config)
    orchestrator = PipelineOrchestrator(config)

    if args.command == "run":
        result = orchestrator.run_mvp_collection()
        print(json.dumps({"run_id": result.run_id, "status": result.status}, indent=2))
        return 0

    if args.command == "test-llm":
        print(json.dumps(orchestrator.test_llm(), indent=2))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2
