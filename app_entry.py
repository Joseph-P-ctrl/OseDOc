from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

WORKSPACE_DIR = Path(__file__).resolve().parent


def _run_sync() -> int:
    """Run the data synchronization script."""
    script_path = WORKSPACE_DIR / "osinergmin_auth.py"
    cmd = [sys.executable, str(script_path)]
    result = subprocess.run(cmd, cwd=str(WORKSPACE_DIR))
    return int(result.returncode)


def _run_web(host: str, port: int) -> int:
    """Start the FastAPI web app with Uvicorn."""
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "web_app:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    result = subprocess.run(cmd, cwd=str(WORKSPACE_DIR))
    return int(result.returncode)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osidoc",
        description="Simple launcher for OsiDOc tasks.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "sync",
        help="Run synchronization and document download flow.",
    )

    web_parser = subparsers.add_parser(
        "web",
        help="Start the web viewer.",
    )
    web_parser.add_argument("--host", default="127.0.0.1", help="Host for web server.")
    web_parser.add_argument("--port", type=int, default=8010, help="Port for web server.")

    return parser


def run(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "sync":
        return _run_sync()

    if args.command == "web":
        return _run_web(host=args.host, port=args.port)

    parser.print_help()
    return 2
