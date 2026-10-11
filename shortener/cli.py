"""Command line entry point for server and workflow demos."""

import argparse
import json

from .config import Settings
from .database import Database
from .orchestrator import SCENARIOS, WorkflowEngine
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentic-shortener")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the HTTP API")
    demo = commands.add_parser("demo", help="run an engineering workflow scenario")
    demo.add_argument("scenario", choices=SCENARIOS)
    demo.add_argument("--approve", action="store_true", help="approve the release gate as a human operator")
    demo.add_argument("--inject-failure", choices=["requirements", "design", "implementation", "tests", "documentation", "validation"])
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "serve":
        serve(settings)
        return
    database = Database(settings.database_path)
    database.initialize()
    result = WorkflowEngine(database, settings.max_retries).start(
        args.scenario, approve_release=args.approve, fail_once=args.inject_failure
    )
    print(json.dumps(result, indent=2))
    if result["status"] == "awaiting_approval":
        print("\nHuman approval required. Approve with POST /api/workflows/{run_id}/approve.")


if __name__ == "__main__":
    main()
