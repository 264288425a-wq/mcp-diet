"""Baseline snapshot serialization and PR token-budget diff gate."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp_diet.analyzer import analyze_manifest
from mcp_diet.models import AuditReport, DiffReport, ServerManifest, ToolDiff
from mcp_diet.tokenizer import TokenCounter, get_token_counter

SNAPSHOT_SCHEMA_VERSION = 1


def build_snapshot(
    manifest_or_report: ServerManifest | AuditReport,
    *,
    counter: TokenCounter | None = None,
) -> dict[str, Any]:
    """Build a canonical JSON-serializable budget snapshot from a manifest or audit report."""
    report = (
        manifest_or_report
        if isinstance(manifest_or_report, AuditReport)
        else analyze_manifest(manifest_or_report, counter=counter)
    )
    tools_map = {t.name: t.total_tokens for t in sorted(report.tools, key=lambda x: x.name)}
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server_name": report.server_name,
        "tokenizer": report.tokenizer_name,
        "totals": {
            "total_tokens": report.total_tokens,
            "tools_tokens": report.tools_tokens,
            "resources_tokens": report.resources_tokens,
            "prompts_tokens": report.prompts_tokens,
            "tool_count": report.tool_count,
        },
        "tools": tools_map,
    }


def save_snapshot(snapshot: dict[str, Any], path: str | Path) -> Path:
    """Write a snapshot dictionary to disk as formatted JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def load_snapshot(path: str | Path) -> dict[str, Any]:
    """Load and validate a baseline snapshot JSON file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "tools" not in raw or "totals" not in raw:
        raise ValueError(f"Invalid mcp-diet snapshot file: {path}")
    return raw


def diff_snapshots(
    baseline: dict[str, Any],
    current: dict[str, Any] | ServerManifest | AuditReport,
    *,
    max_increase: int | None = None,
    max_total: int | None = None,
    counter: TokenCounter | None = None,
) -> DiffReport:
    """Compare a baseline snapshot against a current snapshot, manifest, or audit report."""
    if isinstance(current, (ServerManifest, AuditReport)):
        current_snap = build_snapshot(current, counter=counter or get_token_counter())
    else:
        current_snap = current

    before_tools: dict[str, int] = {
        str(k): int(v) for k, v in dict(baseline.get("tools", {})).items()
    }
    after_tools: dict[str, int] = {
        str(k): int(v) for k, v in dict(current_snap.get("tools", {})).items()
    }

    before_total = int(baseline.get("totals", {}).get("total_tokens", sum(before_tools.values())))
    after_total = int(current_snap.get("totals", {}).get("total_tokens", sum(after_tools.values())))

    all_names = sorted(set(before_tools) | set(after_tools))
    tool_diffs: list[ToolDiff] = []

    for name in all_names:
        in_before = name in before_tools
        in_after = name in after_tools
        b_tok = before_tools.get(name, 0)
        a_tok = after_tools.get(name, 0)

        if not in_before and in_after:
            status = "added"
        elif in_before and not in_after:
            status = "removed"
        elif b_tok != a_tok:
            status = "changed"
        else:
            status = "unchanged"

        tool_diffs.append(
            ToolDiff(name=name, status=status, before_tokens=b_tok, after_tokens=a_tok)
        )

    # Sort by largest absolute delta first, keeping changed/added/removed ahead of unchanged
    tool_diffs.sort(key=lambda d: (d.status == "unchanged", -abs(d.delta), d.name))

    delta = after_total - before_total
    failures: list[str] = []
    if max_increase is not None and delta > max_increase:
        failures.append(
            f"Net token increase +{delta} exceeds allowed --max-increase ({max_increase})."
        )
    if max_total is not None and after_total > max_total:
        failures.append(
            f"Total tokens {after_total} exceeds allowed --max-total ({max_total})."
        )

    server_name = str(
        current_snap.get("server_name") or baseline.get("server_name") or "mcp-server"
    )
    tokenizer_name = str(current_snap.get("tokenizer") or baseline.get("tokenizer") or "unknown")

    return DiffReport(
        server_name=server_name,
        tokenizer_name=tokenizer_name,
        before_total=before_total,
        after_total=after_total,
        max_increase=max_increase,
        max_total=max_total,
        passed=not failures,
        failure_reasons=failures,
        tool_diffs=tool_diffs,
    )
