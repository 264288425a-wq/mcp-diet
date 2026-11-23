"""mcp-diet: Context token budget analyzer, CI diff gate, and slimming proxy for MCP servers."""

from __future__ import annotations

from mcp_diet.analyzer import analyze_manifest
from mcp_diet.diff_gate import build_snapshot, diff_snapshots
from mcp_diet.models import (
    AuditReport,
    BudgetPolicy,
    DiffReport,
    Finding,
    PromptItem,
    ResourceItem,
    ServerManifest,
    Severity,
    ToolItem,
    ToolMetrics,
    TrimConfig,
    TrimResult,
)
from mcp_diet.tokenizer import TokenCounter, get_token_counter
from mcp_diet.trimmer import compact_schema, trim_mcp_call_result, trim_payload, trim_response

__version__ = "0.1.0"

__all__ = [
    "AuditReport",
    "BudgetPolicy",
    "DiffReport",
    "Finding",
    "PromptItem",
    "ResourceItem",
    "ServerManifest",
    "Severity",
    "TokenCounter",
    "ToolItem",
    "ToolMetrics",
    "TrimConfig",
    "TrimResult",
    "__version__",
    "analyze_manifest",
    "build_snapshot",
    "compact_schema",
    "diff_snapshots",
    "get_token_counter",
    "trim_mcp_call_result",
    "trim_payload",
    "trim_response",
]
