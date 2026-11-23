"""Schema minification and runtime MCP payload slimming utilities."""

from __future__ import annotations

import copy
import functools
import inspect
import json
import re
from collections.abc import Callable
from typing import Any, TypeVar

from mcp_diet.models import ToolItem, TrimConfig, TrimResult
from mcp_diet.tokenizer import TokenCounter, get_token_counter

_WS_RE = re.compile(r"[ \t]{2,}")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_MAX_SCHEMA_RECURSION = 64

F = TypeVar("F", bound=Callable[..., Any])


def _compact_description(text: str, max_chars: int = 240) -> str:
    """Normalize whitespace and trim overly verbose multi-paragraph descriptions."""
    cleaned = _BLANK_LINES_RE.sub("\n\n", text.strip())
    cleaned = _WS_RE.sub(" ", cleaned)
    if len(cleaned) <= max_chars:
        return cleaned

    head = cleaned[: max(0, max_chars - 3)].rstrip()
    last_space = head.rfind(" ")
    if last_space > max_chars // 2:
        head = head[:last_space].rstrip(" ,;:-")
    return f"{head}..."


def _compact_schema_node(
    node: dict[str, Any],
    strip_titles: bool = True,
    max_prop_desc: int = 120,
    _depth: int = 0,
) -> dict[str, Any]:
    """Minify annotation keywords on known JSON Schema nodes without altering validation rules."""
    out = dict(node)
    if _depth >= _MAX_SCHEMA_RECURSION:
        return out

    if strip_titles:
        out.pop("title", None)

    description = out.get("description")
    if isinstance(description, str):
        compacted_desc = _compact_description(description, max_chars=max_prop_desc)
        if compacted_desc:
            out["description"] = compacted_desc
        else:
            out.pop("description", None)

    examples = out.get("examples")
    if isinstance(examples, list) and len(examples) > 1:
        out["examples"] = examples[:1]

    for map_key in ("properties", "patternProperties", "$defs", "definitions"):
        mapping = out.get(map_key)
        if isinstance(mapping, dict):
            out[map_key] = {
                name: (
                    _compact_schema_node(sub, strip_titles, max_prop_desc, _depth + 1)
                    if isinstance(sub, dict)
                    else sub
                )
                for name, sub in mapping.items()
            }

    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _compact_schema_node(items, strip_titles, max_prop_desc, _depth + 1)
    elif isinstance(items, list):
        out["items"] = [
            (
                _compact_schema_node(sub, strip_titles, max_prop_desc, _depth + 1)
                if isinstance(sub, dict)
                else sub
            )
            for sub in items
        ]

    for list_key in ("prefixItems", "anyOf", "oneOf", "allOf"):
        branches = out.get(list_key)
        if isinstance(branches, list):
            out[list_key] = [
                (
                    _compact_schema_node(sub, strip_titles, max_prop_desc, _depth + 1)
                    if isinstance(sub, dict)
                    else sub
                )
                for sub in branches
            ]

    # Preserve boolean additionalProperties verbatim; only recurse when it is a sub-schema dict
    additional = out.get("additionalProperties")
    if isinstance(additional, dict):
        out["additionalProperties"] = _compact_schema_node(
            additional, strip_titles, max_prop_desc, _depth + 1
        )

    negated = out.get("not")
    if isinstance(negated, dict):
        out["not"] = _compact_schema_node(negated, strip_titles, max_prop_desc, _depth + 1)

    return out


def compact_schema(
    tool: ToolItem | dict[str, Any],
    *,
    max_description_chars: int = 240,
    max_property_description_chars: int = 120,
    strip_titles: bool = True,
) -> dict[str, Any]:
    """Return a compacted MCP tool dict preserving outputSchema and all validation keywords."""
    raw = tool.to_mcp_dict() if isinstance(tool, ToolItem) else copy.deepcopy(tool)
    result: dict[str, Any] = dict(raw)

    desc = result.get("description")
    if isinstance(desc, str):
        compacted_desc = _compact_description(desc, max_chars=max_description_chars)
        if compacted_desc:
            result["description"] = compacted_desc

    schema = result.get("inputSchema")
    if isinstance(schema, dict):
        result["inputSchema"] = _compact_schema_node(
            schema,
            strip_titles=strip_titles,
            max_prop_desc=max_property_description_chars,
        )
    return result


def _truncate_str(value: str, max_chars: int) -> tuple[str, bool]:
    """Truncate a string so the returned length (including ellipsis) never exceeds `max_chars`."""
    cap = max(0, max_chars)
    if len(value) <= cap:
        return value, False
    if cap < 8:
        return value[:cap], True
    tail = (cap - 3) // 4
    head = cap - 3 - tail
    if tail == 0:
        return f"{value[:cap - 3]}...", True
    return f"{value[:head]}...{value[-tail:]}", True


def _prune_value(
    value: Any,
    *,
    max_list_items: int,
    max_string_chars: int,
    max_depth: int,
    drop_nulls: bool,
    drop_empty: bool,
    strip_keys: frozenset[str],
    pagination_hint: str,
    actions: set[str],
    depth: int = 0,
) -> Any:
    if isinstance(value, str):
        trimmed, changed = _truncate_str(value, max_string_chars)
        if changed:
            actions.add("truncated_strings")
        return trimmed

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if depth >= max_depth:
        actions.add("capped_nesting_depth")
        if isinstance(value, list):
            return {"_trimmed": True, "type": "list", "length": len(value)}
        if isinstance(value, dict):
            return {"_trimmed": True, "type": "object", "keys": list(value.keys())[:8]}
        return _truncate_str(str(value), max_string_chars)[0]

    if isinstance(value, list):
        total = len(value)
        sliced = value[:max_list_items]
        processed = [
            _prune_value(
                item,
                max_list_items=max_list_items,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                drop_nulls=drop_nulls,
                drop_empty=drop_empty,
                strip_keys=strip_keys,
                pagination_hint=pagination_hint,
                actions=actions,
                depth=depth + 1,
            )
            for item in sliced
        ]
        if total > max_list_items:
            actions.add("truncated_lists")
            return {
                "_truncated": True,
                "shown": len(processed),
                "total": total,
                "omitted": total - len(processed),
                "hint": pagination_hint,
                "items": processed,
            }
        return processed

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if key_str in strip_keys:
                actions.add("stripped_noisy_keys")
                continue
            if drop_nulls and v is None:
                actions.add("dropped_nulls")
                continue
            if drop_empty and v in ("", [], {}):
                actions.add("dropped_empty")
                continue
            cleaned_v = _prune_value(
                v,
                max_list_items=max_list_items,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                drop_nulls=drop_nulls,
                drop_empty=drop_empty,
                strip_keys=strip_keys,
                pagination_hint=pagination_hint,
                actions=actions,
                depth=depth + 1,
            )
            if drop_empty and cleaned_v in ("", [], {}):
                actions.add("dropped_empty")
                continue
            out[key_str] = cleaned_v
        return out

    return _truncate_str(str(value), max_string_chars)[0]


def trim_payload(
    payload: Any,
    config: TrimConfig | None = None,
    *,
    counter: TokenCounter | None = None,
) -> TrimResult:
    """Iteratively slim an arbitrary JSON/text payload to fit within `config.max_tokens`."""
    cfg = config or TrimConfig()
    tok = counter or get_token_counter()
    is_text = isinstance(payload, str)

    original_tokens = tok.count_text(payload) if is_text else tok.count_json(payload)
    if original_tokens <= cfg.max_tokens and not cfg.drop_nulls and not cfg.strip_keys:
        return TrimResult(
            payload=payload,
            original_tokens=original_tokens,
            trimmed_tokens=original_tokens,
            was_trimmed=False,
        )

    actions: set[str] = set()
    strip_keys = frozenset(cfg.strip_keys)

    list_cap = cfg.max_list_items
    str_cap = cfg.max_string_chars
    depth_cap = cfg.max_depth

    candidate = _prune_value(
        payload,
        max_list_items=list_cap,
        max_string_chars=str_cap,
        max_depth=depth_cap,
        drop_nulls=cfg.drop_nulls,
        drop_empty=cfg.drop_empty,
        strip_keys=strip_keys,
        pagination_hint=cfg.pagination_hint,
        actions=actions,
    )
    current_tokens = (
        tok.count_text(candidate) if isinstance(candidate, str) else tok.count_json(candidate)
    )

    # Iterative structural tightening
    attempts = 0
    while current_tokens > cfg.max_tokens and attempts < 6:
        attempts += 1
        list_cap = max(3, list_cap // 2)
        str_cap = max(60, int(str_cap * 0.6))
        depth_cap = max(2, depth_cap - 1)
        actions.add("iterative_budget_tightening")
        candidate = _prune_value(
            payload,
            max_list_items=list_cap,
            max_string_chars=str_cap,
            max_depth=depth_cap,
            drop_nulls=True,
            drop_empty=True,
            strip_keys=strip_keys,
            pagination_hint=cfg.pagination_hint,
            actions=actions,
        )
        current_tokens = (
            tok.count_text(candidate) if isinstance(candidate, str) else tok.count_json(candidate)
        )

    # Measured hard-cap loop so final output genuinely fits within cfg.max_tokens
    if current_tokens > cfg.max_tokens:
        actions.add("hard_token_cap")
        serialized = (
            candidate
            if isinstance(candidate, str)
            else json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        )
        size = len(serialized) // 2
        fitted = False
        while size > 0:
            preview, _ = _truncate_str(serialized, size)
            trial: Any = preview if is_text else {"_truncated": True, "preview": preview}
            trial_tokens = tok.count_text(trial) if is_text else tok.count_json(trial)
            if trial_tokens <= cfg.max_tokens:
                candidate = trial
                current_tokens = trial_tokens
                fitted = True
                break
            size //= 2

        if not fitted:
            candidate = "" if is_text else ([] if isinstance(payload, list) else {})
            current_tokens = tok.count_text("") if is_text else tok.count_json(candidate)
            if current_tokens > cfg.max_tokens:
                actions.add("budget_unattainable")

    was_trimmed = candidate != payload
    return TrimResult(
        payload=candidate,
        original_tokens=original_tokens,
        trimmed_tokens=current_tokens,
        was_trimmed=was_trimmed,
        actions=sorted(actions),
    )


def trim_mcp_call_result(
    result: dict[str, Any],
    config: TrimConfig | None = None,
    *,
    counter: TokenCounter | None = None,
) -> tuple[dict[str, Any], TrimResult]:
    """Slim an MCP `tools/call` CallToolResult while preserving protocol validity."""
    cfg = config or TrimConfig()
    tok = counter or get_token_counter()

    original_tokens = tok.count_json(result)
    if original_tokens <= cfg.max_tokens:
        return result, TrimResult(
            payload=result,
            original_tokens=original_tokens,
            trimmed_tokens=original_tokens,
            was_trimmed=False,
        )

    content = result.get("content")
    if not isinstance(content, list) or not content or "structuredContent" in result:
        return result, TrimResult(
            payload=result,
            original_tokens=original_tokens,
            trimmed_tokens=original_tokens,
            was_trimmed=False,
            actions=["budget_unattainable"],
        )

    per_item_budget = max(0, cfg.max_tokens // len(content))
    new_content: list[Any] = []
    actions: set[str] = set()

    for block in content:
        if not isinstance(block, dict):
            new_content.append(block)
            continue
        block_copy = dict(block)
        if block_copy.get("type") == "text" and isinstance(block_copy.get("text"), str):
            raw_text = block_copy["text"]
            parsed_json: Any = None
            is_json = False
            skip_block = False
            stripped = raw_text.lstrip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed_json = json.loads(raw_text)
                    is_json = True
                except json.JSONDecodeError:
                    is_json = False
                except RecursionError:
                    skip_block = True
                    actions.add("skipped_unparseable_json")

            if not skip_block:
                block_cfg = TrimConfig(
                    max_tokens=per_item_budget,
                    max_list_items=cfg.max_list_items,
                    max_string_chars=cfg.max_string_chars,
                    max_depth=cfg.max_depth,
                    drop_nulls=cfg.drop_nulls,
                    drop_empty=cfg.drop_empty,
                    strip_keys=cfg.strip_keys,
                    pagination_hint=cfg.pagination_hint,
                )
                if is_json:
                    res = trim_payload(parsed_json, block_cfg, counter=tok)
                    block_copy["text"] = json.dumps(
                        res.payload, ensure_ascii=False, separators=(",", ":")
                    )
                    actions.update(res.actions)
                else:
                    res = trim_payload(raw_text, block_cfg, counter=tok)
                    block_copy["text"] = str(res.payload)
                    actions.update(res.actions)
        new_content.append(block_copy)

    updated = dict(result)
    updated["content"] = new_content
    trimmed_tokens = tok.count_json(updated)

    # Enforce whole-envelope budget by omitting trailing text blocks before giving up
    while trimmed_tokens > cfg.max_tokens:
        idx = next(
            (
                i
                for i in range(len(new_content) - 1, -1, -1)
                if isinstance(new_content[i], dict) and new_content[i].get("type") == "text"
            ),
            None,
        )
        if idx is None:
            actions.add("budget_unattainable")
            break
        del new_content[idx]
        actions.add("omitted_text_blocks")
        trimmed_tokens = tok.count_json(updated)

    was_trimmed = updated != result
    return updated, TrimResult(
        payload=updated,
        original_tokens=original_tokens,
        trimmed_tokens=trimmed_tokens,
        was_trimmed=was_trimmed,
        actions=sorted(actions),
    )


def trim_response(
    *,
    max_tokens: int = 1500,
    max_list_items: int = 25,
    max_string_chars: int = 600,
    max_depth: int = 6,
    drop_nulls: bool = True,
    drop_empty: bool = False,
    strip_keys: tuple[str, ...] | None = None,
    pagination_hint: str | None = None,
) -> Callable[[F], F]:
    """Decorator that automatically trims oversized return values from sync or async tools.

    Works seamlessly with FastMCP tools, OpenAI Agents SDK tools, or plain Python functions.
    """
    default_cfg = TrimConfig()
    cfg = TrimConfig(
        max_tokens=max_tokens,
        max_list_items=max_list_items,
        max_string_chars=max_string_chars,
        max_depth=max_depth,
        drop_nulls=drop_nulls,
        drop_empty=drop_empty,
        strip_keys=strip_keys if strip_keys is not None else default_cfg.strip_keys,
        pagination_hint=pagination_hint or default_cfg.pagination_hint,
    )

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                raw_result = await func(*args, **kwargs)
                return trim_payload(raw_result, cfg).payload

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            raw_result = func(*args, **kwargs)
            return trim_payload(raw_result, cfg).payload

        return sync_wrapper  # type: ignore[return-value]

    return decorator
