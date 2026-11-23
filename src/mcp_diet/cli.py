"""Command-line interface for mcp-diet."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp_diet import __version__
from mcp_diet.analyzer import analyze_manifest
from mcp_diet.client import (
    discover_config_servers,
    inspect_http_server,
    inspect_stdio_server,
    load_manifest_from_file,
)
from mcp_diet.diff_gate import build_snapshot, diff_snapshots, load_snapshot, save_snapshot
from mcp_diet.models import BudgetPolicy, ServerManifest, Severity, ToolItem, TrimConfig
from mcp_diet.proxy import run_stdio_proxy
from mcp_diet.reporters import (
    render_diff_json,
    render_diff_markdown,
    render_diff_terminal,
    render_json,
    render_markdown,
    render_sarif,
    render_terminal,
    supports_color,
)
from mcp_diet.tokenizer import get_token_counter


def _build_demo_manifest() -> ServerManifest:
    """Built-in realistic sample MCP manifest illustrating common context bloat patterns."""
    shared_filter = {
        "type": "object",
        "title": "AdvancedQueryFilter",
        "description": "Complex nested filter criteria applied to repository items.",
        "properties": {
            "dateRange": {
                "type": "object",
                "title": "DateRange",
                "properties": {
                    "bounds": {
                        "type": "object",
                        "properties": {
                            "window": {
                                "type": "object",
                                "properties": {
                                    "startIso": {"type": "string", "title": "Start ISO"},
                                    "endIso": {"type": "string", "title": "End ISO"},
                                },
                            }
                        },
                    }
                },
            },
            "includeArchived": {"type": "boolean", "title": "Include Archived"},
            "labels": {"type": "array", "items": {"type": "string"}, "title": "Labels"},
        },
    }
    country_enum = [f"REGION_{i:02d}" for i in range(1, 31)]

    tools = [
        ToolItem.from_mcp_dict(
            {
                "name": "search_issues_advanced",
                "description": (
                    "Search GitHub issues across all accessible organizations and repositories "
                    "using rich boolean queries, date range windows, label filters, assignee "
                    "constraints, milestone matching, and sort order preferences. "
                    "IMPORTANT: Always prefer narrow queries first, verify permissions before "
                    "calling, inspect pagination cursors carefully, and consult the API docs "
                    "when combining multiple filters. Example usage: call with repository name, "
                    "open state, priority labels, and explicit sorting criteria to retrieve "
                    "matching issue threads along with full metadata payloads. " * 3
                ),
                "inputSchema": {
                    "type": "object",
                    "title": "SearchIssuesInput",
                    "properties": {
                        "owner": {"type": "string", "title": "Owner"},
                        "repo": {"type": "string", "title": "Repo"},
                        "query": {"type": "string", "title": "Query"},
                        "region": {"type": "string", "title": "Region", "enum": country_enum},
                        "filter": shared_filter,
                        "sort": {"type": "string", "title": "Sort"},
                        "order": {"type": "string", "title": "Order"},
                        "page": {"type": "integer", "title": "Page"},
                        "per_page": {"type": "integer", "title": "Per Page"},
                        "include_comments": {"type": "boolean", "title": "Include Comments"},
                        "include_events": {"type": "boolean", "title": "Include Events"},
                        "include_reactions": {"type": "boolean", "title": "Include Reactions"},
                        "raw_markdown": {"type": "boolean", "title": "Raw Markdown"},
                    },
                    "required": ["owner", "repo"],
                },
            }
        ),
        ToolItem.from_mcp_dict(
            {
                "name": "list_pull_requests",
                "description": "List pull requests in a repository with optional filter criteria.",
                "inputSchema": {
                    "type": "object",
                    "title": "ListPRInput",
                    "properties": {
                        "owner": {"type": "string", "title": "Owner"},
                        "repo": {"type": "string", "title": "Repo"},
                        "filter": shared_filter,
                    },
                    "required": ["owner", "repo"],
                },
            }
        ),
        ToolItem.from_mcp_dict(
            {
                "name": "get_file_contents",
                "description": "Read a single file from a repository at a given ref.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "path": {"type": "string"},
                        "ref": {"type": "string"},
                    },
                    "required": ["owner", "repo", "path"],
                },
            }
        ),
    ]
    return ServerManifest(server_name="demo-github-mcp", source="built-in://demo", tools=tools)


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--file", help="Path to an offline MCP tools manifest JSON file.")
    group.add_argument("--cmd", help="Command string to launch a live stdio MCP server.")
    group.add_argument("--url", help="Streamable HTTP endpoint URL of a live MCP server.")
    group.add_argument(
        "--config",
        help="Path to mcp.json, claude_desktop_config.json, or Codex config.toml.",
    )
    parser.add_argument(
        "--server",
        help="When using --config, inspect only the named server.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Timeout in seconds for live stdio/HTTP inspection (default: 15).",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra HTTP header in 'Key: Value' format for --url (repeatable).",
    )


def _add_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-total", type=int, default=4000, help="Max total menu tokens.")
    parser.add_argument("--max-tool", type=int, default=500, help="Max tokens for one tool.")
    parser.add_argument(
        "--max-description",
        type=int,
        default=180,
        help="Max tokens for one tool description.",
    )
    parser.add_argument("--max-depth", type=int, default=4, help="Max inputSchema nesting depth.")
    parser.add_argument("--max-enum", type=int, default=20, help="Max enum items per property.")
    parser.add_argument("--max-params", type=int, default=12, help="Max top-level parameters.")
    parser.add_argument(
        "--context-window",
        type=int,
        default=200_000,
        help="Reference LLM context window size for menu-tax percentage (default: 200000).",
    )
    parser.add_argument(
        "--tokenizer",
        choices=("auto", "heuristic", "tiktoken"),
        default="auto",
        help="Token counter backend (default: auto).",
    )


def _parse_headers(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw_headers:
        if ":" not in item:
            raise ValueError(f"Invalid --header format (expected 'Key: Value'): {item}")
        k, v = item.split(":", 1)
        headers[k.strip()] = v.strip()
    return headers


def _resolve_counter(mode: str) -> Any:
    if mode == "heuristic":
        return get_token_counter(prefer_tiktoken=False)
    if mode == "tiktoken":
        from mcp_diet.tokenizer import TiktokenCounter

        return TiktokenCounter()
    return get_token_counter(prefer_tiktoken=True)


def _load_manifest_from_args(args: argparse.Namespace) -> ServerManifest:
    if getattr(args, "file", None):
        return load_manifest_from_file(args.file)
    if getattr(args, "cmd", None):
        return inspect_stdio_server(args.cmd, timeout=args.timeout)
    if getattr(args, "url", None):
        headers = _parse_headers(args.header)
        return inspect_http_server(args.url, headers=headers, timeout=args.timeout)
    if getattr(args, "config", None):
        servers = discover_config_servers(args.config)
        if not servers:
            raise ValueError(f"No MCP servers found in {args.config}")
        if args.server:
            matches = [s for s in servers if s.name == args.server]
            if not matches:
                available = ", ".join(s.name for s in servers)
                raise ValueError(f"Server '{args.server}' not found. Available: {available}")
            target = matches[0]
        else:
            target = servers[0]
        if target.transport == "http":
            return inspect_http_server(target.url, server_name=target.name, timeout=args.timeout)
        return inspect_stdio_server(
            target.command,
            server_name=target.name,
            timeout=args.timeout,
            env=target.env,
        )
    raise ValueError("Specify one of --file, --cmd, --url, or --config.")


def _write_output(text: str, output_path: str | None) -> None:
    if output_path:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    else:
        sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))


def _policy_from_args(args: argparse.Namespace) -> BudgetPolicy:
    return BudgetPolicy(
        max_total_tokens=args.max_total,
        max_tool_tokens=args.max_tool,
        max_description_tokens=args.max_description,
        max_schema_depth=args.max_depth,
        max_enum_items=args.max_enum,
        max_parameters=args.max_params,
        context_window=args.context_window,
    )


def _cmd_inspect_or_check(args: argparse.Namespace, enforce_gate: bool) -> int:
    if getattr(args, "discover_only", False):
        if not args.config:
            raise ValueError("--discover-only requires --config.")
        servers = discover_config_servers(args.config)
        payload = [
            {
                "name": s.name,
                "transport": s.transport,
                "command": s.command,
                "url": s.url,
            }
            for s in servers
        ]
        _write_output(json.dumps(payload, indent=2, ensure_ascii=False), args.output)
        return 0

    manifest = _load_manifest_from_args(args)
    counter = _resolve_counter(args.tokenizer)
    report = analyze_manifest(manifest, _policy_from_args(args), counter=counter)

    fmt = args.format
    if fmt == "json":
        rendered = render_json(report)
    elif fmt == "markdown":
        rendered = render_markdown(report)
    elif fmt == "sarif":
        artifact_uri: str | None = None
        if args.file:
            path = Path(args.file)
            artifact_uri = path.as_uri() if path.is_absolute() else quote(path.as_posix(), safe="/")
        rendered = render_sarif(report, artifact_uri=artifact_uri)
    else:
        rendered = render_terminal(report, color=(supports_color() and not args.output))

    _write_output(rendered, args.output)

    if enforce_gate:
        fail_on_str = args.fail_on
        if fail_on_str == "none":
            return 0
        threshold = Severity(fail_on_str)
        return 0 if report.passes(threshold) else 1
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    manifest = _load_manifest_from_args(args)
    counter = _resolve_counter(args.tokenizer)
    snap = build_snapshot(manifest, counter=counter)
    if args.output:
        save_snapshot(snap, args.output)
        sys.stderr.write(
            f"Saved snapshot for '{snap['server_name']}' "
            f"({snap['totals']['total_tokens']:,} tokens) -> {args.output}\n"
        )
    else:
        _write_output(json.dumps(snap, indent=2, ensure_ascii=False), None)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    baseline = load_snapshot(args.baseline)
    counter = _resolve_counter(args.tokenizer)

    if args.current_snapshot:
        current: Any = load_snapshot(args.current_snapshot)
    else:
        current = _load_manifest_from_args(args)

    diff = diff_snapshots(
        baseline,
        current,
        max_increase=args.max_increase,
        max_total=args.max_total,
        counter=counter,
    )
    if args.format == "json":
        rendered = render_diff_json(diff)
    elif args.format == "markdown":
        rendered = render_diff_markdown(diff)
    else:
        rendered = render_diff_terminal(diff, color=(supports_color() and not args.output))

    _write_output(rendered, args.output)
    return 0 if diff.passed else 1


def _cmd_proxy(args: argparse.Namespace) -> int:
    upstream = list(args.upstream_cmd)
    if upstream and upstream[0] == "--":
        upstream = upstream[1:]
    if not upstream:
        raise ValueError(
            "Provide an upstream MCP command after `--`, "
            "e.g. `mcp-diet proxy -- python server.py`."
        )

    cfg = TrimConfig(
        max_tokens=args.max_response_tokens,
        max_list_items=args.max_list_items,
        max_string_chars=args.max_string_chars,
        drop_nulls=not args.keep_nulls,
    )
    return run_stdio_proxy(
        upstream,
        compact_schemas=args.compact_schemas,
        trim_config=cfg,
        verbose=args.verbose,
    )


def _cmd_demo(args: argparse.Namespace) -> int:
    manifest = _build_demo_manifest()
    counter = _resolve_counter(args.tokenizer)
    policy = BudgetPolicy(max_total_tokens=900, max_tool_tokens=450)
    report = analyze_manifest(manifest, policy, counter=counter)
    if args.format == "markdown":
        rendered = render_markdown(report)
    elif args.format == "json":
        rendered = render_json(report)
    elif args.format == "sarif":
        rendered = render_sarif(report)
    else:
        rendered = render_terminal(report, color=supports_color())
    _write_output(rendered, None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-diet",
        description=(
            "Context token budget analyzer, PR diff gate, "
            "and slimming proxy for MCP servers."
        ),
    )
    parser.add_argument("--version", action="version", version=f"mcp-diet {__version__}")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # inspect
    p_inspect = sub.add_parser("inspect", help="Audit an MCP server or manifest and print report.")
    _add_source_args(p_inspect)
    _add_policy_args(p_inspect)
    p_inspect.add_argument(
        "--discover-only",
        action="store_true",
        help="List servers found in --config without launching them.",
    )
    p_inspect.add_argument(
        "--format",
        choices=("terminal", "markdown", "json", "sarif"),
        default="terminal",
    )
    p_inspect.add_argument("-o", "--output", help="Write report to a file instead of stdout.")

    # check
    p_check = sub.add_parser("check", help="Run budget & lint gate (non-zero exit on violations).")
    _add_source_args(p_check)
    _add_policy_args(p_check)
    p_check.add_argument(
        "--fail-on",
        choices=("none", "info", "warning", "error"),
        default="error",
        help="Minimum finding severity that fails the gate (default: error).",
    )
    p_check.add_argument(
        "--format",
        choices=("terminal", "markdown", "json", "sarif"),
        default="terminal",
    )
    p_check.add_argument("-o", "--output", help="Write report to a file instead of stdout.")

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Capture a baseline token budget snapshot JSON.")
    _add_source_args(p_snap)
    p_snap.add_argument(
        "--tokenizer",
        choices=("auto", "heuristic", "tiktoken"),
        default="auto",
    )
    p_snap.add_argument("-o", "--output", help="Path to write snapshot JSON.")

    # diff
    p_diff = sub.add_parser("diff", help="Compare current server/snapshot against a baseline.")
    p_diff.add_argument("--baseline", required=True, help="Path to baseline snapshot JSON.")
    p_diff.add_argument(
        "--current-snapshot",
        help="Optional path to a second snapshot JSON instead of --file/--cmd/--url.",
    )
    _add_source_args(p_diff)
    p_diff.add_argument("--max-increase", type=int, default=None, help="Max allowed token delta.")
    p_diff.add_argument("--max-total", type=int, default=None, help="Max allowed total tokens.")
    p_diff.add_argument(
        "--tokenizer",
        choices=("auto", "heuristic", "tiktoken"),
        default="auto",
    )
    p_diff.add_argument(
        "--format",
        choices=("terminal", "markdown", "json"),
        default="terminal",
    )
    p_diff.add_argument("-o", "--output", help="Write diff report to file.")

    # proxy
    p_proxy = sub.add_parser("proxy", help="Run a transparent stdio MCP slimming proxy.")
    p_proxy.add_argument(
        "--compact-schemas",
        action="store_true",
        default=False,
        help="Compact bloated tool schemas in tools/list responses.",
    )
    p_proxy.add_argument(
        "--max-response-tokens",
        type=int,
        default=1500,
        help="Max tokens allowed per tools/call response (0 disables trimming, default: 1500).",
    )
    p_proxy.add_argument("--max-list-items", type=int, default=25)
    p_proxy.add_argument("--max-string-chars", type=int, default=600)
    p_proxy.add_argument("--keep-nulls", action="store_true", default=False)
    p_proxy.add_argument("-v", "--verbose", action="store_true", help="Log savings to stderr.")
    p_proxy.add_argument("upstream_cmd", nargs=argparse.REMAINDER, help="Upstream server command.")

    # demo
    p_demo = sub.add_parser("demo", help="Run a self-contained demo audit with sample bloat.")
    p_demo.add_argument(
        "--format",
        choices=("terminal", "markdown", "json", "sarif"),
        default="terminal",
    )
    p_demo.add_argument(
        "--tokenizer",
        choices=("auto", "heuristic", "tiktoken"),
        default="auto",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.subcommand == "inspect":
            return _cmd_inspect_or_check(args, enforce_gate=False)
        if args.subcommand == "check":
            return _cmd_inspect_or_check(args, enforce_gate=True)
        if args.subcommand == "snapshot":
            return _cmd_snapshot(args)
        if args.subcommand == "diff":
            return _cmd_diff(args)
        if args.subcommand == "proxy":
            return _cmd_proxy(args)
        if args.subcommand == "demo":
            return _cmd_demo(args)
        parser.print_help()
        return 2
    except Exception as exc:
        sys.stderr.write(f"mcp-diet error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
