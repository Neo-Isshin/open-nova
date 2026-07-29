"""Actanara command line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys

from .external_agent_memory import DEFAULT_SEARCH_TIMEOUT_SECONDS, compact_memory_results, search_memory
from .version import product_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="actanara",
        description="Search your Actanara memory.",
    )
    subcommands = parser.add_subparsers(dest="command")

    rag = subcommands.add_parser("rag", help="Detailed memory-search commands")
    rag_subcommands = rag.add_subparsers(dest="rag_command")
    rag.set_defaults(rag_parser=rag)
    search = rag_subcommands.add_parser(
        "search-memory",
        help="Search your Actanara memory.",
    )
    search.add_argument("query", help="Words or question to search for")
    search.add_argument("--top-k", type=int, default=5, help="Maximum number of results, up to 20")
    search.add_argument("--dashboard-url", default=None, help="Use a specific Dashboard URL")
    search.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_SEARCH_TIMEOUT_SECONDS,
        help="Seconds to wait for results",
    )
    search.add_argument("--date", default="", help="Search one date")
    search.add_argument("--date-from", default="", help="Search from this date")
    search.add_argument("--date-to", default="", help="Search through this date")
    search.add_argument("--project", default="", help="Search one project")
    search.add_argument("--role", default="", help="Search one assistant or role")
    search.add_argument("--source-set", action="append", default=[], help="Search one kind of memory; may be repeated")
    search.add_argument("--caller", default="", help="Agent requesting recall, for example codex or claude-code")
    search.add_argument("--json", action="store_true", help="Print JSON for scripts and automation")
    return parser


def main(argv: list[str] | None = None) -> int:
    selected_args = list(argv) if argv is not None else sys.argv[1:]
    if selected_args == ["--version"]:
        print(f"actanara {product_version()}")
        return 0
    if not selected_args or selected_args[0] != "rag":
        from .operator_cli import main as operator_main

        return operator_main(selected_args)
    parser = build_parser()
    args = parser.parse_args(selected_args)
    if args.command == "rag" and args.rag_command == "search-memory":
        filters = {
            "date": args.date,
            "dateFrom": args.date_from,
            "dateTo": args.date_to,
            "project": args.project,
            "role": args.role,
            "sourceSets": args.source_set,
        }
        try:
            result = search_memory(
                args.query,
                top_k=args.top_k,
                dashboard_url=args.dashboard_url,
                timeout_seconds=args.timeout,
                filters=filters,
                mode="rag",
                caller=args.caller or None,
            )
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(compact_memory_results(result, max_results=args.top_k))
        return 0
    if args.command == "rag":
        args.rag_parser.print_help()
        return 1
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
