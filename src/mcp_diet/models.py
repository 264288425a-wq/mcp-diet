"""Core strongly typed data models for mcp-diet."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Diagnostic finding severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    def rank(self) -> int:
        """Numeric ordering for fail-on threshold checks."""
        return {Severity.INFO: 1, Severity.WARNING: 2, Severity.ERROR: 3}[self]


@dataclass(slots=True)
class ToolItem:
    """Representation of an MCP tool from `tools/list`."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mcp_dict(cls, data: dict[str, Any]) -> ToolItem:
        """Build a ToolItem from a raw MCP JSON-RPC tool dictionary."""
        name = str(data.get("name", "")).strip() or "<unnamed>"
        description = str(data.get("description") or "")
        input_schema = data.get("inputSchema")
        if not isinstance(input_schema, dict):
            alt_schema = data.get("input_schema")
            input_schema = alt_schema if isinstance(alt_schema, dict) else {}
        annotations = data.get("annotations")
        if not isinstance(annotations, dict):
            annotations = {}
        return cls(
            name=name,
            description=description,
            input_schema=input_schema,
            annotations=annotations,
            raw=dict(data),
        )

    def to_mcp_dict(self) -> dict[str, Any]:
        """Serialize back into canonical MCP tool JSON format while preserving extra fields."""
        payload: dict[str, Any] = dict(self.raw) if self.raw else {}
        payload["name"] = self.name
        payload["description"] = self.description
        payload["inputSchema"] = self.input_schema
        if self.annotations:
            payload["annotations"] = self.annotations
        return payload


@dataclass(slots=True)
class ResourceItem:
    """Representation of an MCP resource from `resources/list`."""

    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""

    @classmethod
    def from_mcp_dict(cls, data: dict[str, Any]) -> ResourceItem:
        return cls(
            uri=str(data.get("uri", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description") or ""),
            mime_type=str(data.get("mimeType") or data.get("mime_type") or ""),
        )

    def to_mcp_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"uri": self.uri, "name": self.name}
        if self.description:
            out["description"] = self.description
        if self.mime_type:
            out["mimeType"] = self.mime_type
        return out


@dataclass(slots=True)
class PromptItem:
    """Representation of an MCP prompt from `prompts/list`."""

    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_mcp_dict(cls, data: dict[str, Any]) -> PromptItem:
        args = data.get("arguments")
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description") or ""),
            arguments=list(args) if isinstance(args, list) else [],
        )

    def to_mcp_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.description:
            out["description"] = self.description
        if self.arguments:
            out["arguments"] = self.arguments
        return out


@dataclass(slots=True)
class ServerManifest:
    """Collected MCP server capabilities and declarations."""

    server_name: str
    source: str
    server_version: str = ""
    protocol_version: str = ""
    tools: list[ToolItem] = field(default_factory=list)
    resources: list[ResourceItem] = field(default_factory=list)
    prompts: list[PromptItem] = field(default_factory=list)


@dataclass(slots=True)
class BudgetPolicy:
    """Configurable context token budget thresholds and lint limits."""

    max_total_tokens: int = 4000
    max_tool_tokens: int = 500
    max_description_tokens: int = 180
    max_schema_depth: int = 4
    max_enum_items: int = 20
    max_parameters: int = 12
    context_window: int = 200_000


@dataclass(slots=True)
class Finding:
    """Single actionable lint finding."""

    rule_id: str
    severity: Severity
    message: str
    suggestion: str
    tool_name: str | None = None
    tokens: int = 0
    estimated_savings: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "tool_name": self.tool_name,
            "message": self.message,
            "suggestion": self.suggestion,
            "tokens": self.tokens,
            "estimated_savings": self.estimated_savings,
        }


@dataclass(slots=True)
class ToolMetrics:
    """Computed token and structural metrics for a single MCP tool."""

    name: str
    total_tokens: int
    description_tokens: int
    schema_tokens: int
    compact_tokens: int
    parameter_count: int
    max_depth: int
    largest_enum: int
    share_pct: float = 0.0

    @property
    def potential_savings(self) -> int:
        return max(0, self.total_tokens - self.compact_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_tokens": self.total_tokens,
            "description_tokens": self.description_tokens,
            "schema_tokens": self.schema_tokens,
            "compact_tokens": self.compact_tokens,
            "potential_savings": self.potential_savings,
            "parameter_count": self.parameter_count,
            "max_depth": self.max_depth,
            "largest_enum": self.largest_enum,
            "share_pct": round(self.share_pct, 2),
        }


@dataclass(slots=True)
class AuditReport:
    """Full audit report for an MCP server manifest."""

    server_name: str
    source: str
    tokenizer_name: str
    tool_count: int
    resource_count: int
    prompt_count: int
    tools_tokens: int
    resources_tokens: int
    prompts_tokens: int
    total_tokens: int
    compact_total_tokens: int
    context_window: int
    menu_tax_pct: float
    policy: BudgetPolicy
    tools: list[ToolMetrics] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def potential_savings(self) -> int:
        return max(0, self.total_tokens - self.compact_total_tokens)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.WARNING)

    def passes(self, fail_on: Severity | None = Severity.ERROR) -> bool:
        """Return True if no finding meets or exceeds `fail_on` severity."""
        if fail_on is None:
            return True
        threshold = fail_on.rank()
        return all(f.severity.rank() < threshold for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "source": self.source,
            "tokenizer": self.tokenizer_name,
            "summary": {
                "tool_count": self.tool_count,
                "resource_count": self.resource_count,
                "prompt_count": self.prompt_count,
                "tools_tokens": self.tools_tokens,
                "resources_tokens": self.resources_tokens,
                "prompts_tokens": self.prompts_tokens,
                "total_tokens": self.total_tokens,
                "compact_total_tokens": self.compact_total_tokens,
                "potential_savings": self.potential_savings,
                "context_window": self.context_window,
                "menu_tax_pct": round(self.menu_tax_pct, 3),
                "errors": self.error_count,
                "warnings": self.warning_count,
            },
            "tools": [t.to_dict() for t in self.tools],
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(slots=True)
class ToolDiff:
    """Token delta for a single tool between baseline and current snapshots."""

    name: str
    status: str  # "added" | "removed" | "changed" | "unchanged"
    before_tokens: int
    after_tokens: int

    @property
    def delta(self) -> int:
        return self.after_tokens - self.before_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "delta": self.delta,
        }


@dataclass(slots=True)
class DiffReport:
    """Comparison report between a baseline snapshot and a current manifest/snapshot."""

    server_name: str
    tokenizer_name: str
    before_total: int
    after_total: int
    max_increase: int | None
    max_total: int | None
    passed: bool
    failure_reasons: list[str] = field(default_factory=list)
    tool_diffs: list[ToolDiff] = field(default_factory=list)

    @property
    def delta(self) -> int:
        return self.after_total - self.before_total

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "tokenizer": self.tokenizer_name,
            "before_total": self.before_total,
            "after_total": self.after_total,
            "delta": self.delta,
            "max_increase": self.max_increase,
            "max_total": self.max_total,
            "passed": self.passed,
            "failure_reasons": list(self.failure_reasons),
            "tools": [td.to_dict() for td in self.tool_diffs],
        }


@dataclass(slots=True)
class TrimConfig:
    """Configuration controlling runtime payload slimming."""

    max_tokens: int = 1500
    max_list_items: int = 25
    max_string_chars: int = 600
    max_depth: int = 6
    drop_nulls: bool = True
    drop_empty: bool = False
    strip_keys: tuple[str, ...] = (
        "_links",
        "avatar_url",
        "gravatar_id",
        "node_id",
        "html_raw",
    )
    pagination_hint: str = "Output trimmed by mcp-diet; narrow filters or paginate for more."


@dataclass(slots=True)
class TrimResult:
    """Result of trimming a runtime payload."""

    payload: Any
    original_tokens: int
    trimmed_tokens: int
    was_trimmed: bool
    actions: list[str] = field(default_factory=list)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.trimmed_tokens)

    @property
    def reduction_pct(self) -> float:
        if self.original_tokens <= 0:
            return 0.0
        return (self.saved_tokens / self.original_tokens) * 100.0
