"""Command-line interface for building and exporting ToDAG documents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import ToDAGEngine


def _read_json(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input document must be a JSON object")
    return value


def _write_json(value: object, output: str | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert a structured long task to a dynamic DAG")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build and validate a DAG")
    build_parser.add_argument("input", help="input JSON document")
    build_parser.add_argument("--horizon", type=int, default=10, help="rolling planning horizon (default: 10)")
    build_parser.add_argument("--output", "-o", help="write the DAG JSON to this file")
    build_parser.add_argument(
        "--execution-output",
        help="also write the execution-boundary task list to this file",
    )
    serve_parser = subparsers.add_parser("serve", help="run the API and dynamic visualization")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8780)
    serve_parser.add_argument("--input", help="optional JSON input document loaded on startup")
    args = parser.parse_args()

    if args.command == "serve":
        from .web import run

        run(args.host, args.port, args.input)
        return

    engine = ToDAGEngine(planning_horizon=args.horizon)
    result = engine.build(_read_json(args.input))
    _write_json(result, args.output)
    if args.execution_output:
        _write_json({"tasks": engine.execution_plan()}, args.execution_output)


if __name__ == "__main__":
    main()
