"""MCP context bloat analyzer implementing lint rules DIET001 through DIET007."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from mcp_diet.models import (
    AuditReport,
    BudgetPolicy,
    Finding,
    ServerManifest,
    Severity,
    ToolItem,
    ToolMetrics,
)
from mcp_diet.tokenizer import TokenCounter, get_token_counter
from mcp_diet.trimmer import compact_schema

RULE_METADATA: dict[str, dict[str, str]] = {
    "DIET001": {
        "name": "OversizedToolDefinition",
        "short": "Single MCP tool definition exceeds per-tool token budget.",
        "help": (
            "Split multi-purpose tools, shorten verbose docstrings, "
            "or remove redundant schema keys."
        ),
    },
    "DIET002": {
        "name": "VerboseToolDescription",
        "short": "Tool description consumes excessive tokens.",
        "help": "Keep tool descriptions to 1-2 crisp sentences focused on when to call the tool.",
    },
    "DIET003": {
        "name": "DeeplyNestedSchema",
        "short": "Tool inputSchema nesting depth exceeds recommended maximum.",
        "help": (
            "Flatten deeply nested objects to improve tool-calling accuracy "
            "and cut schema tokens."
        ),
    },
    "DIET004": {
        "name": "LargeEnumBloat",
        "short": "Tool inputSchema contains an oversized enum list.",
        "help": (
            "Replace huge inline enums with string validation, pattern hints, "
            "or a dedicated lookup tool."
        ),
    },
    "DIET005": {
        "name": "DuplicateSubSchema",
        "short": "Identical large sub-schema is repeated across multiple tools.",
        "help": "Extract shared fields into simpler identifiers or consolidate overlapping tools.",
    },
    "DIET006": {
        "name": "ServerMenuBudgetExceeded",
        "short": "Total MCP server startup menu tokens exceed the configured budget.",
        "help": "Trim tool schemas, merge redundant tools, or enable progressive tool discovery.",
    },
    "DIET007": {
        "name": "ToolParameterOrDescriptionSmell",
        "short": "Tool defines too many top-level parameters or lacks a description.",
        "help": (
            "Reduce parameters to essential inputs (<= 12) and ensure every tool "
            "has a concise description."
        ),
    },
}


def _schema_max_depth(node: Any, depth: int = 0) -> int:
    """Compute structural nesting depth of a JSON Schema properties/items tree."""
    if not isinstance(node, dict):
        return depth

    best = depth
    properties = node.get("properties")
    if isinstance(properties, dict):
        for prop_schema in properties.values():
            best = max(best, _schema_max_depth(prop_schema, depth + 1))

    items = node.get("items")
    if isinstance(items, dict):
        best = max(best, _schema_max_depth(items, depth + 1))
    elif isinstance(items, list):
        for item in items:
            best = max(best, _schema_max_depth(item, depth + 1))

    for combiner in ("anyOf", "oneOf", "allOf"):
        branches = node.get(combiner)
        if isinstance(branches, list):
            for branch in branches:
                best = max(best, _schema_max_depth(branch, depth + 1))

    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        best = max(best, _schema_max_depth(additional, depth + 1))

    return best


def _find_large_enums(node: Any, path: str = "") -> list[tuple[str, int]]:
    """Return `(property_path, enum_length)` pairs for all enum arrays in a JSON Schema."""
    results: list[tuple[str, int]] = []
    if isinstance(node, dict):
        enum_val = node.get("enum")
        if isinstance(enum_val, list):
            results.append((path or "<root>", len(enum_val)))
        properties = node.get("properties")
        if isinstance(properties, dict):
            for prop_name, prop_schema in properties.items():
                next_path = f"{path}.{prop_name}" if path else str(prop_name)
                results.extend(_find_large_enums(prop_schema, next_path))
        items = node.get("items")
        if isinstance(items, dict):
            results.extend(_find_large_enums(items, f"{path}[]"))
        for combiner in ("anyOf", "oneOf", "allOf"):
            branches = node.get(combiner)
            if isinstance(branches, list):
                for idx, branch in enumerate(branches):
                    results.extend(_find_large_enums(branch, f"{path}[{combiner}:{idx}]"))
    return results


def _collect_subschema_hashes(
    tool: ToolItem,
    counter: TokenCounter,
    min_tokens: int = 60,
) -> list[tuple[str, str, int]]:
    """Collect `(hash, prop_name, tokens)` for substantial object/array sub-schemas."""
    found: list[tuple[str, str, int]] = []
    properties = tool.input_schema.get("properties")
    if not isinstance(properties, dict):
        return found

    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        tok_count = counter.count_json(prop_schema)
        if tok_count < min_tokens:
            continue
        canonical = json.dumps(prop_schema, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        found.append((digest, str(prop_name), tok_count))
    return found


def analyze_manifest(
    manifest: ServerManifest,
    policy: BudgetPolicy | None = None,
    *,
    counter: TokenCounter | None = None,
) -> AuditReport:
    """Audit an MCP ServerManifest for context token bloat and produce an AuditReport."""
    pol = policy or BudgetPolicy()
    tok = counter or get_token_counter()

    tool_metrics: list[ToolMetrics] = []
    findings: list[Finding] = []
    subschema_owners: dict[str, list[tuple[str, str, int]]] = defaultdict(list)

    tools_tokens = 0
    compact_tools_tokens = 0

    for tool in manifest.tools:
        tool_dict = tool.to_mcp_dict()
        total_t = tok.count_json(tool_dict)
        desc_t = tok.count_text(tool.description)
        schema_t = tok.count_json(tool.input_schema)
        compact_t = tok.count_json(compact_schema(tool))

        props = tool.input_schema.get("properties")
        param_count = len(props) if isinstance(props, dict) else 0
        max_depth = _schema_max_depth(tool.input_schema)
        enums = _find_large_enums(tool.input_schema)
        largest_enum = max((length for _, length in enums), default=0)

        tools_tokens += total_t
        compact_tools_tokens += compact_t

        metrics = ToolMetrics(
            name=tool.name,
            total_tokens=total_t,
            description_tokens=desc_t,
            schema_tokens=schema_t,
            compact_tokens=compact_t,
            parameter_count=param_count,
            max_depth=max_depth,
            largest_enum=largest_enum,
        )
        tool_metrics.append(metrics)

        # DIET001: Oversized single tool
        if total_t > pol.max_tool_tokens:
            findings.append(
                Finding(
                    rule_id="DIET001",
                    severity=Severity.ERROR,
                    tool_name=tool.name,
                    tokens=total_t,
                    estimated_savings=max(total_t - pol.max_tool_tokens, metrics.potential_savings),
                    message=(
                        f"Tool '{tool.name}' uses {total_t} tokens "
                        f"(budget: {pol.max_tool_tokens})."
                    ),
                    suggestion=(
                        "Trim verbose docstrings, strip redundant Pydantic titles, or split "
                        "large multi-action schemas."
                    ),
                )
            )

        # DIET002: Verbose description
        if desc_t > pol.max_description_tokens:
            findings.append(
                Finding(
                    rule_id="DIET002",
                    severity=Severity.WARNING,
                    tool_name=tool.name,
                    tokens=desc_t,
                    estimated_savings=max(0, desc_t - min(80, pol.max_description_tokens // 2)),
                    message=(
                        f"Tool '{tool.name}' description uses {desc_t} tokens "
                        f"(limit: {pol.max_description_tokens})."
                    ),
                    suggestion=(
                        "Condense description to 1-2 action-oriented sentences and move lengthy "
                        "usage guides to docs or an on-demand resource."
                    ),
                )
            )

        # DIET003: Deep schema nesting
        if max_depth > pol.max_schema_depth:
            findings.append(
                Finding(
                    rule_id="DIET003",
                    severity=Severity.WARNING,
                    tool_name=tool.name,
                    tokens=schema_t,
                    estimated_savings=int(schema_t * 0.2),
                    message=(
                        f"Tool '{tool.name}' inputSchema nesting depth is {max_depth} "
                        f"(limit: {pol.max_schema_depth})."
                    ),
                    suggestion=(
                        "Flatten nested filter/config objects into top-level primitive parameters "
                        "to reduce tokens and improve LLM parameter accuracy."
                    ),
                )
            )

        # DIET004: Large inline enum
        for enum_path, enum_len in enums:
            if enum_len > pol.max_enum_items:
                findings.append(
                    Finding(
                        rule_id="DIET004",
                        severity=Severity.WARNING,
                        tool_name=tool.name,
                        tokens=enum_len * 2,
                        estimated_savings=max(0, (enum_len - 5) * 2),
                        message=(
                            f"Tool '{tool.name}' parameter '{enum_path}' has an enum with "
                            f"{enum_len} items (limit: {pol.max_enum_items})."
                        ),
                        suggestion=(
                            "Replace large static enums with a concise string description plus "
                            "server-side validation or a dynamic list tool."
                        ),
                    )
                )

        # DIET007: Too many parameters or empty description
        if param_count > pol.max_parameters:
            findings.append(
                Finding(
                    rule_id="DIET007",
                    severity=Severity.WARNING,
                    tool_name=tool.name,
                    tokens=schema_t,
                    estimated_savings=int(schema_t * 0.25),
                    message=(
                        f"Tool '{tool.name}' exposes {param_count} top-level parameters "
                        f"(limit: {pol.max_parameters})."
                    ),
                    suggestion=(
                        "Hide rarely used flags behind sensible defaults or split specialized "
                        "workflows into focused tools."
                    ),
                )
            )
        if not tool.description.strip():
            findings.append(
                Finding(
                    rule_id="DIET007",
                    severity=Severity.INFO,
                    tool_name=tool.name,
                    tokens=0,
                    estimated_savings=0,
                    message=f"Tool '{tool.name}' has an empty description.",
                    suggestion=(
                        "Add a short 1-sentence description so agents select the tool reliably "
                        "without trial-and-error retries."
                    ),
                )
            )

        for digest, prop_name, prop_tokens in _collect_subschema_hashes(tool, tok):
            subschema_owners[digest].append((tool.name, prop_name, prop_tokens))

    # DIET005: Repeated identical sub-schemas across tools
    for _digest, occurrences in subschema_owners.items():
        unique_tools = sorted({t_name for t_name, _, _ in occurrences})
        if len(unique_tools) >= 2:
            sample_prop = occurrences[0][1]
            unit_tokens = occurrences[0][2]
            wasted = unit_tokens * (len(occurrences) - 1)
            findings.append(
                Finding(
                    rule_id="DIET005",
                    severity=Severity.INFO,
                    tool_name=", ".join(unique_tools[:3]),
                    tokens=unit_tokens * len(occurrences),
                    estimated_savings=wasted,
                    message=(
                        f"Identical sub-schema '{sample_prop}' ({unit_tokens} tokens) is repeated "
                        f"across {len(unique_tools)} tools ({', '.join(unique_tools[:4])})."
                    ),
                    suggestion=(
                        "Consolidate overlapping tools or simplify shared filter/auth objects "
                        "into compact identifiers."
                    ),
                )
            )

    resources_tokens = (
        tok.count_json([r.to_mcp_dict() for r in manifest.resources]) if manifest.resources else 0
    )
    prompts_tokens = (
        tok.count_json([p.to_mcp_dict() for p in manifest.prompts]) if manifest.prompts else 0
    )

    total_tokens = tools_tokens + resources_tokens + prompts_tokens
    compact_total_tokens = compact_tools_tokens + resources_tokens + prompts_tokens

    # Populate share percentages and sort tools descending by token footprint
    for m in tool_metrics:
        m.share_pct = (m.total_tokens / total_tokens * 100.0) if total_tokens > 0 else 0.0
    tool_metrics.sort(key=lambda item: item.total_tokens, reverse=True)

    # DIET006: Total menu budget exceeded
    if total_tokens > pol.max_total_tokens:
        findings.append(
            Finding(
                rule_id="DIET006",
                severity=Severity.ERROR,
                tool_name=None,
                tokens=total_tokens,
                estimated_savings=max(
                    total_tokens - pol.max_total_tokens,
                    total_tokens - compact_total_tokens,
                ),
                message=(
                    f"Server '{manifest.server_name}' startup menu uses {total_tokens} tokens "
                    f"(budget: {pol.max_total_tokens})."
                ),
                suggestion=(
                    "Run `mcp-diet proxy --compact-schemas` or trim the heaviest tools listed "
                    "above to stay within budget."
                ),
            )
        )

    # Sort findings: errors first, then warnings, then info, highest tokens first
    findings.sort(key=lambda f: (-f.severity.rank(), -f.tokens, f.rule_id))

    menu_tax_pct = (total_tokens / pol.context_window * 100.0) if pol.context_window > 0 else 0.0

    return AuditReport(
        server_name=manifest.server_name,
        source=manifest.source,
        tokenizer_name=tok.name,
        tool_count=len(manifest.tools),
        resource_count=len(manifest.resources),
        prompt_count=len(manifest.prompts),
        tools_tokens=tools_tokens,
        resources_tokens=resources_tokens,
        prompts_tokens=prompts_tokens,
        total_tokens=total_tokens,
        compact_total_tokens=compact_total_tokens,
        context_window=pol.context_window,
        menu_tax_pct=menu_tax_pct,
        policy=pol,
        tools=tool_metrics,
        findings=findings,
    )
