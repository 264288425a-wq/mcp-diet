"""Output formatters: terminal bar charts, GitHub Markdown, JSON, and SARIF 2.1.0."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp_diet.analyzer import RULE_METADATA
from mcp_diet.models import AuditReport, DiffReport, Severity
from mcp_diet.tokenizer import estimate_cost_per_1k_turns

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_RED = "\033[31m"
_ANSI_YELLOW = "\033[33m"
_ANSI_GREEN = "\033[32m"
_ANSI_CYAN = "\033[36m"


def supports_color() -> bool:
    """Detect whether ANSI color output should be enabled."""
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _style(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_ANSI_RESET}" if enabled else text


def _bar(ratio: float, width: int = 18) -> str:
    clamped = max(0.0, min(1.0, ratio))
    filled = int(round(clamped * width))
    return "#" * filled + "." * (width - filled)


def render_terminal(
    report: AuditReport,
    *,
    color: bool = False,
    top_n: int = 15,
) -> str:
    """Render a crisp, terminal-friendly audit summary with horizontal token bars."""
    lines: list[str] = []
    header = f"mcp-diet Audit Report — {report.server_name}"
    lines.append(_style(header, _ANSI_BOLD + _ANSI_CYAN, color))
    lines.append(_style("=" * len(header), _ANSI_DIM, color))
    lines.append(
        f"Source: {report.source}  |  Tokenizer: {report.tokenizer_name}  |  "
        f"Tools: {report.tool_count}  Resources: {report.resource_count}  "
        f"Prompts: {report.prompt_count}"
    )

    est_cost = estimate_cost_per_1k_turns(report.total_tokens)
    savings_pct = (
        (report.potential_savings / report.total_tokens * 100.0) if report.total_tokens > 0 else 0.0
    )
    budget_ok = report.total_tokens <= report.policy.max_total_tokens
    status_tag = _style("PASS", _ANSI_GREEN + _ANSI_BOLD, color) if budget_ok else _style(
        "OVER BUDGET", _ANSI_RED + _ANSI_BOLD, color
    )

    lines.append(
        f"Total Menu Tax: {_style(f'{report.total_tokens:,} tokens', _ANSI_BOLD, color)} "
        f"({report.menu_tax_pct:.2f}% of {report.context_window // 1000}k window)  "
        f"[{status_tag} vs {report.policy.max_total_tokens:,} limit]"
    )
    lines.append(
        f"Compactable Target: {report.compact_total_tokens:,} tokens "
        f"(potential savings: {report.potential_savings:,} tokens / {savings_pct:.1f}%)  |  "
        f"Est. ~${est_cost:.2f} / 1k turns (gpt-5 input)"
    )
    lines.append("")

    if report.tools:
        shown = report.tools[:top_n]
        max_tok = max((t.total_tokens for t in shown), default=1) or 1
        name_w = max(12, min(32, max(len(t.name) for t in shown)))
        lines.append(_style("Top Tools by Context Footprint:", _ANSI_BOLD, color))
        lines.append(
            f"  {'Tool':<{name_w}}  {'Tokens':>7}  {'Desc':>6}  "
            f"{'Save':>6}  {'Share':>6}  Distribution"
        )
        lines.append("  " + "-" * (name_w + 52))
        for tool in shown:
            display_name = (
                tool.name if len(tool.name) <= name_w else tool.name[: name_w - 1] + "~"
            )
            bar_str = _bar(tool.total_tokens / max_tok)
            tok_col = f"{tool.total_tokens:>7,}"
            if tool.total_tokens > report.policy.max_tool_tokens:
                tok_col = _style(tok_col, _ANSI_RED, color)
            lines.append(
                f"  {display_name:<{name_w}}  {tok_col}  {tool.description_tokens:>6,}  "
                f"{tool.potential_savings:>6,}  {tool.share_pct:>5.1f}%  [{bar_str}]"
            )
        if len(report.tools) > top_n:
            lines.append(f"  ... and {len(report.tools) - top_n} more tools")
        lines.append("")

    if report.findings:
        lines.append(
            _style(
                f"Findings ({report.error_count} errors, {report.warning_count} warnings, "
                f"{len(report.findings) - report.error_count - report.warning_count} info):",
                _ANSI_BOLD,
                color,
            )
        )
        for finding in report.findings:
            sev_color = {
                Severity.ERROR: _ANSI_RED,
                Severity.WARNING: _ANSI_YELLOW,
                Severity.INFO: _ANSI_CYAN,
            }[finding.severity]
            badge = _style(f"[{finding.severity.value.upper():<7}]", sev_color, color)
            lines.append(f"  {badge} {finding.rule_id}: {finding.message}")
            lines.append(f"            Fix: {finding.suggestion}")
    else:
        lines.append(_style("No context bloat issues detected.", _ANSI_GREEN, color))

    return "\n".join(lines)


def render_markdown(report: AuditReport, *, top_n: int = 12) -> str:
    """Render a GitHub PR / Job Summary Markdown report."""
    status_emoji = "PASS" if report.passes(Severity.ERROR) else "FAIL"
    savings_pct = (
        (report.potential_savings / report.total_tokens * 100.0) if report.total_tokens > 0 else 0.0
    )

    lines: list[str] = [
        f"## mcp-diet Context Budget Report: `{report.server_name}` ({status_emoji})",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        (
            f"| **Total Menu Tokens** | `{report.total_tokens:,}` "
            f"(budget: `{report.policy.max_total_tokens:,}`) |"
        ),
        (
            f"| **Context Window Share** | `{report.menu_tax_pct:.2f}%` "
            f"of `{report.context_window // 1000}k` |"
        ),
        (
            f"| **Compactable Potential** | `{report.compact_total_tokens:,}` "
            f"(save `{report.potential_savings:,}` / `{savings_pct:.1f}%`) |"
        ),
        (
            f"| **Inventory** | `{report.tool_count}` tools, "
            f"`{report.resource_count}` resources, `{report.prompt_count}` prompts |"
        ),
        f"| **Findings** | `{report.error_count}` errors, `{report.warning_count}` warnings |",
        f"| **Tokenizer** | `{report.tokenizer_name}` |",
        "",
    ]

    if report.tools:
        lines.append("### Heaviest Tools")
        lines.append("")
        lines.append("| Tool | Total Tokens | Desc Tokens | Params | Depth | Potential Savings |")
        lines.append("| :--- | ---: | ---: | ---: | ---: | ---: |")
        for t in report.tools[:top_n]:
            lines.append(
                f"| `{t.name}` | {t.total_tokens:,} | {t.description_tokens:,} | "
                f"{t.parameter_count} | {t.max_depth} | -{t.potential_savings:,} |"
            )
        lines.append("")

    if report.findings:
        lines.append("### Diagnostics")
        lines.append("")
        for f in report.findings:
            lines.append(
                f"- **{f.rule_id}** (`{f.severity.value}`): "
                f"{f.message} — _{f.suggestion}_"
            )
        lines.append("")

    return "\n".join(lines)


def render_json(report: AuditReport) -> str:
    """Render AuditReport as formatted JSON."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)


def render_sarif(report: AuditReport, *, artifact_uri: str | None = None) -> str:
    """Render findings as a valid SARIF 2.1.0 document for GitHub Code Scanning."""
    level_map = {
        Severity.ERROR: "error",
        Severity.WARNING: "warning",
        Severity.INFO: "note",
    }
    rules: list[dict[str, Any]] = []
    for rule_id, meta in sorted(RULE_METADATA.items()):
        rules.append(
            {
                "id": rule_id,
                "name": meta["name"],
                "shortDescription": {"text": meta["short"]},
                "help": {"text": meta["help"]},
            }
        )

    results: list[dict[str, Any]] = []
    for f in report.findings:
        location: dict[str, Any] = {}
        if artifact_uri:
            location["physicalLocation"] = {"artifactLocation": {"uri": artifact_uri}}
        if f.tool_name:
            location["logicalLocations"] = [{"name": f.tool_name, "kind": "function"}]
        if not location:
            location["message"] = {"text": f"Finding on MCP server {report.server_name}"}

        results.append(
            {
                "ruleId": f.rule_id,
                "level": level_map[f.severity],
                "message": {"text": f"{f.message} Suggestion: {f.suggestion}"},
                "locations": [location],
                "properties": {
                    "tokens": f.tokens,
                    "estimatedSavings": f.estimated_savings,
                },
            }
        )

    sarif: dict[str, Any] = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "mcp-diet",
                        "version": "0.1.0",
                        "informationUri": "https://github.com/264288425a-wq/mcp-diet",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(sarif, indent=2, ensure_ascii=False)


def render_diff_terminal(diff: DiffReport, *, color: bool = False) -> str:
    """Render a snapshot comparison report for terminal output."""
    sign = "+" if diff.delta > 0 else ""
    status = _style("PASS", _ANSI_GREEN + _ANSI_BOLD, color) if diff.passed else _style(
        "FAIL", _ANSI_RED + _ANSI_BOLD, color
    )
    lines = [
        _style(f"mcp-diet Snapshot Diff — {diff.server_name} [{status}]", _ANSI_BOLD, color),
        f"Total Tokens: {diff.before_total:,} -> {diff.after_total:,} ({sign}{diff.delta:,})",
    ]
    if diff.max_increase is not None:
        lines.append(f"Allowed Increase: +{diff.max_increase:,}")
    if diff.max_total is not None:
        lines.append(f"Allowed Total:    {diff.max_total:,}")
    lines.append("")

    changed = [td for td in diff.tool_diffs if td.status != "unchanged"]
    if changed:
        lines.append(f"  {'Tool':<30}  {'Status':<9}  {'Before':>7}  {'After':>7}  {'Delta':>8}")
        lines.append("  " + "-" * 70)
        for td in changed:
            d_sign = "+" if td.delta > 0 else ""
            lines.append(
                f"  {td.name:<30}  {td.status:<9}  {td.before_tokens:>7,}  "
                f"{td.after_tokens:>7,}  {d_sign}{td.delta:>7,}"
            )
    else:
        lines.append("  No tool token changes detected.")

    if diff.failure_reasons:
        lines.append("")
        for reason in diff.failure_reasons:
            lines.append(_style(f"ERROR: {reason}", _ANSI_RED, color))
    return "\n".join(lines)


def render_diff_markdown(diff: DiffReport) -> str:
    """Render a PR-ready Markdown token diff table."""
    sign = "+" if diff.delta > 0 else ""
    badge = "PASS" if diff.passed else "FAIL"
    lines = [
        f"## mcp-diet PR Token Diff: `{diff.server_name}` ({badge})",
        "",
        f"- **Baseline:** `{diff.before_total:,}` tokens",
        f"- **Current:** `{diff.after_total:,}` tokens (**`{sign}{diff.delta:,}`**)",
    ]
    if diff.max_increase is not None:
        lines.append(f"- **Max Allowed Increase:** `+{diff.max_increase:,}` tokens")
    if diff.max_total is not None:
        lines.append(f"- **Max Allowed Total:** `{diff.max_total:,}` tokens")
    lines.append("")

    changed = [td for td in diff.tool_diffs if td.status != "unchanged"]
    if changed:
        lines.append("| Tool | Status | Before | After | Delta |")
        lines.append("| :--- | :--- | ---: | ---: | ---: |")
        for td in changed:
            d_sign = "+" if td.delta > 0 else ""
            lines.append(
                f"| `{td.name}` | `{td.status}` | {td.before_tokens:,} | "
                f"{td.after_tokens:,} | `{d_sign}{td.delta:,}` |"
            )
        lines.append("")
    else:
        lines.append("_No tool token changes detected._\n")

    if diff.failure_reasons:
        lines.append("### Gate Failures")
        for reason in diff.failure_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    return "\n".join(lines)


def render_diff_json(diff: DiffReport) -> str:
    return json.dumps(diff.to_dict(), indent=2, ensure_ascii=False)
