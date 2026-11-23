"""Minimal zero-dependency stdio MCP server for local demos and E2E testing.

Exposes:
- A bloated tool schema (useful for testing `mcp-diet inspect --cmd` and `--compact-schemas`)
- `fetch_huge_logs`: returns a 150-item JSON array with noisy fields (useful for testing
  `mcp-diet proxy --max-response-tokens ...`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("bloated_tools.json")


def _load_tools() -> list[dict[str, Any]]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    tools = list(data.get("tools", []))
    tools.append(
        {
            "name": "fetch_huge_logs",
            "description": "Return a deliberately oversized JSON payload for proxy trimming tests.",
            "inputSchema": {
                "type": "object",
                "title": "FetchHugeLogsInput",
                "properties": {
                    "count": {"type": "integer", "title": "Count", "default": 150},
                },
            },
        }
    )
    return tools


def _handle_request(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id")

    # Notifications have no id and expect no reply
    if req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-bloated-mcp", "version": "0.1.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _load_tools()},
        }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "fetch_huge_logs":
            count = int(args.get("count", 150))
            payload = {
                "status": "ok",
                "records": [
                    {
                        "id": i,
                        "message": f"Log entry #{i}: " + ("verbose trace payload " * 12),
                        "avatar_url": f"https://cdn.example.com/avatar/{i}.png",
                        "node_id": f"MDQ6VXNlcj{i:06d}",
                        "nullable_meta": None,
                    }
                    for i in range(count)
                ],
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps({"ok": True, "tool": name})}]},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        resp = _handle_request(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
