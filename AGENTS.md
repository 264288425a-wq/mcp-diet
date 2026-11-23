# AGENTS.md

Instructions for AI coding agents (Codex, Claude Code, Cursor, etc.) contributing to `mcp-diet`.

## Project Overview

`mcp-diet` is a zero-dependency Python 3.10+ CLI, CI gate, and runtime library for auditing and reducing Model Context Protocol (MCP) context token overhead.

## Architecture Map

- `src/mcp_diet/models.py` — Core dataclasses (`ServerManifest`, `ToolItem`, `Finding`, `AuditReport`, `DiffReport`, `TrimConfig`, `TrimResult`).
- `src/mcp_diet/tokenizer.py` — Pluggable token counting (`tiktoken` when installed; calibrated zero-dependency BPE heuristic fallback).
- `src/mcp_diet/analyzer.py` — Context bloat diagnostics (`DIET001`–`DIET007`) and menu-tax metrics.
- `src/mcp_diet/diff_gate.py` — Baseline snapshot serialization, diff engine, and CI budget enforcement.
- `src/mcp_diet/trimmer.py` — Schema minifier (`compact_schema`), iterative JSON/text payload trimmer (`trim_payload`), and `@trim_response` sync/async decorator.
- `src/mcp_diet/client.py` — Zero-dependency JSON-RPC 2.0 MCP client (`stdio`, Streamable HTTP, offline JSON manifest, client config discovery).
- `src/mcp_diet/proxy.py` — Transparent MCP `stdio` proxy that slims `tools/list` schemas and trims `tools/call` responses without polluting stdout framing.
- `src/mcp_diet/reporters.py` — Terminal, GitHub Markdown, JSON, and SARIF 2.1.0 formatters.
- `src/mcp_diet/cli.py` — `argparse` CLI entry point (`mcp-diet`).

## Build & Verification Commands

Run tests using Python's built-in `unittest` (works offline without dev extras):

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

When dev extras are installed:

```bash
pytest
ruff check src tests
mypy src
```

Quick CLI smoke test:

```bash
PYTHONPATH=src python -m mcp_diet demo
```

## Coding Conventions & Constraints

1. **Zero required runtime dependencies**: Do not add third-party packages to `[project.dependencies]` in `pyproject.toml`. Optional dependencies must degrade gracefully via `try/except ImportError`.
2. **Stdio protocol hygiene**: Inside `proxy.py` and `client.py`, never write logs or warnings to `stdout` when handling JSON-RPC over `stdio`; write diagnostic messages strictly to `stderr`.
3. **Typing**: Start every module with `from __future__ import annotations` and keep `mypy --strict` clean.
4. **Testing**: Every new lint rule, CLI flag, or trimming rule must include unit tests in `tests/`. Keep tests deterministic and offline (use `examples/mock_mcp_server.py` for end-to-end `stdio` tests).
