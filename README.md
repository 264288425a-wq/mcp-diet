# mcp-diet

**Put your MCP servers on a diet.**

A zero-dependency Python CLI, GitHub Actions CI gate, and runtime slimming proxy that audits, diffs, and trims Model Context Protocol (MCP) context token bloat.

[![CI](https://github.com/264288425a-wq/mcp-diet/actions/workflows/ci.yml/badge.svg)](https://github.com/264288425a-wq/mcp-diet/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## Why `mcp-diet`?

Connecting coding agents (Codex CLI, Claude Code, Cursor, etc.) to multiple MCP servers creates two silent performance and cost killers:

1. **The Menu Tax (Startup Schema Bloat)**: Loading `tools/list` JSON schemas and multi-paragraph docstrings across 5–10 MCP servers routinely burns **15,000–45,000 tokens** before the agent writes a single line of code.
2. **Response Explosion (Runtime Payload Bloat)**: A single unpaginated tool call returning raw API JSON or HTML dumps can blow out the active context window in one turn.

`mcp-diet` solves both sides in one lightweight package with **zero required runtime dependencies**:

- **Audit (`inspect` / `check`)**: Inspect live `stdio` MCP servers, Streamable HTTP endpoints, local JSON manifests, or client configs (`mcp.json`, `claude_desktop_config.json`, Codex `config.toml`), and flag bloat with 7 actionable lint rules (`DIET001`–`DIET007`).
- **PR Diff Gate (`snapshot` / `diff`)**: Like `size-limit` / `bundlewatch` for MCP servers—track schema token growth on every PR and fail CI when a commit blows your context budget.
- **Runtime Slimming (`proxy` & `@trim_response`)**: Wrap any existing `stdio` MCP server in a transparent slimming proxy, or decorate Python/FastMCP tools to cap oversized payloads with automatic pagination hints.
- **Native GitHub Integration**: Output terminal bar charts, GitHub PR Markdown, JSON, or **SARIF 2.1.0** for GitHub Code Scanning.

---

## Architecture

```text
                       ┌──────────────────────────────────────┐
   Live stdio / HTTP ─▶│  mcp-diet inspect / check / diff     │──▶ Terminal / Markdown / JSON / SARIF
   mcp.json / manifest │  (Tokenizer + DIET001-007 + Gate)    │──▶ GitHub PR Comment & CI Exit Code
                       └──────────────────────────────────────┘

   Coding Agent ◀──stdio──▶ [ mcp-diet proxy ] ◀──stdio──▶ Any Upstream MCP Server
                             ├─ Compacts bloated tools/list schemas
                             └─ Trims oversized tools/call payloads + adds pagination hints
```

---

## Installation

```bash
# Install from this GitHub repository (Python 3.10+)
python -m pip install "mcp-diet @ git+https://github.com/264288425a-wq/mcp-diet.git"

# Optional: exact BPE counting via tiktoken
python -m pip install "mcp-diet[tiktoken] @ git+https://github.com/264288425a-wq/mcp-diet.git"
```

---

## Quick Start

### 1. Try the built-in demo in 1 second

```bash
mcp-diet demo
```

### 2. Inspect a live MCP server (`stdio` or Streamable HTTP)

```bash
# Live stdio server
mcp-diet inspect --cmd "python examples/mock_mcp_server.py"

# Live Streamable HTTP server
mcp-diet inspect --url "http://localhost:8000/mcp"

# Offline manifest JSON
mcp-diet inspect --file examples/bloated_tools.json

# Discover servers from your local MCP config (JSON on Python 3.10+, TOML on Python 3.11+)
mcp-diet inspect --config ~/.codex/config.toml --discover-only
```

### 3. Enforce a CI context budget & PR diff gate

```bash
# Create a baseline snapshot on main
mcp-diet snapshot --file examples/bloated_tools.json --output mcp-budget.snapshot.json

# Fail CI if a PR adds > 250 tokens or exceeds 4,000 total tokens
mcp-diet diff \
  --baseline mcp-budget.snapshot.json \
  --file examples/bloated_tools.json \
  --max-increase 250 \
  --max-total 4000 \
  --format markdown
```

### 4. Put any existing `stdio` MCP server on a runtime diet

Point your MCP host (Codex CLI, Claude Code, Cursor) to `mcp-diet proxy` wrapping the upstream command:

```bash
mcp-diet proxy \
  --compact-schemas \
  --max-response-tokens 1500 \
  -- python examples/mock_mcp_server.py
```

### 5. Trim tool responses inside Python / FastMCP servers

```python
from mcp_diet import trim_response

@trim_response(max_tokens=1200, max_list_items=20, drop_nulls=True)
async def list_repository_issues(owner: str, repo: str) -> dict:
    ...
```

Oversized arrays and nested objects are cleanly truncated with structured metadata (`_truncated`, `shown`, `total`, `hint`) so the agent knows how to paginate instead of hallucinating or crashing on context limits.

---

## GitHub Actions Integration

Drop `mcp-diet` into any MCP server repository to post token budgets directly into the job summary and upload SARIF findings to GitHub Code Scanning:

```yaml
name: MCP Context Budget
on: [pull_request]

jobs:
  mcp-diet:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: 264288425a-wq/mcp-diet@main
        with:
          file: examples/bloated_tools.json
          max-total: "4000"
          max-tool: "500"
          fail-on: "error"
          sarif-output: "mcp-diet.sarif"
```

---

## Built-in Lint Rules

| Rule ID | Severity | What It Detects |
| :--- | :--- | :--- |
| `DIET001` | `error` | Single tool schema + description exceeds `--max-tool` token budget (default: `500`) |
| `DIET002` | `warning` | Verbose tool description exceeds `--max-description` tokens (default: `180`) |
| `DIET003` | `warning` | Deeply nested `inputSchema` (depth > 4), degrading tool-call accuracy and inflating tokens |
| `DIET004` | `warning` | Oversized `enum` list (> 20 items) bloating the startup menu |
| `DIET005` | `info` | Identical sub-schemas duplicated across multiple tools |
| `DIET006` | `error` | Entire server tool menu exceeds `--max-total` token budget (default: `4000`) |
| `DIET007` | `warning` | Excessive parameter count (> 12 parameters on a single tool) or missing description |

---

## Roadmap

- [x] Zero-dependency MCP `stdio` & Streamable HTTP inspection client
- [x] `DIET001`–`DIET007` context bloat rules + SARIF 2.1.0 / Markdown / JSON reporters
- [x] Snapshot & PR diff gate + composite GitHub Action
- [x] Transparent `stdio` slimming proxy & `@trim_response` decorator
- [ ] Built-in benchmark suite comparing top public MCP servers (`mcp-diet bench`)
- [ ] Optional LLM-powered schema rewrite suggestions (`--suggest-rewrites`) via OpenAI API
- [ ] Interactive TUI budget explorer

---

## Contributing & Security

- Read [CONTRIBUTING.md](CONTRIBUTING.md) to set up a local dev environment and run the test suite.
- Read [SECURITY.md](SECURITY.md) for our threat model and vulnerability disclosure process.
- Released under the [MIT License](LICENSE).
