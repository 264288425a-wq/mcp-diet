"""Unit tests for schema compaction, runtime payload trimming, and @trim_response."""

from __future__ import annotations

import inspect
import json
import unittest

from mcp_diet.models import ToolItem, TrimConfig
from mcp_diet.tokenizer import HeuristicTokenCounter
from mcp_diet.trimmer import (
    _truncate_str,
    compact_schema,
    trim_mcp_call_result,
    trim_payload,
    trim_response,
)


class TestTrimmer(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = HeuristicTokenCounter()

    def test_compact_schema_strips_annotation_titles_and_preserves_semantics(self) -> None:
        raw_tool = {
            "name": "list_items",
            "description": ("Very detailed multi-sentence guide for listing items. " * 12),
            "inputSchema": {
                "type": "object",
                "title": "ListItemsInput",
                "additionalProperties": True,
                "properties": {
                    "title": {
                        "type": "integer",
                        "title": "Business Title Field",
                    },
                    "query": {
                        "type": "string",
                        "title": "Query",
                        "description": "Search query string " * 15,
                    },
                },
                "required": ["title"],
                "enum": [{"title": 1}],
            },
            "outputSchema": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
            },
        }
        tool = ToolItem.from_mcp_dict(raw_tool)
        before = self.counter.count_json(tool.to_mcp_dict())
        compacted = compact_schema(tool)
        after = self.counter.count_json(compacted)

        self.assertNotIn("title", compacted["inputSchema"])
        self.assertNotIn("title", compacted["inputSchema"]["properties"]["query"])
        # Business field named "title", enum values, additionalProperties, and outputSchema stay
        self.assertIn("title", compacted["inputSchema"]["properties"])
        self.assertEqual(compacted["inputSchema"]["properties"]["title"]["type"], "integer")
        self.assertNotIn("title", compacted["inputSchema"]["properties"]["title"])
        self.assertEqual(compacted["inputSchema"]["enum"], [{"title": 1}])
        self.assertIs(compacted["inputSchema"]["additionalProperties"], True)
        self.assertEqual(compacted["outputSchema"], raw_tool["outputSchema"])
        self.assertLess(after, before)

    def test_truncate_str_respects_exact_character_cap(self) -> None:
        for cap in (0, 5, 8, 20, 40):
            out, changed = _truncate_str("x" * 60, cap)
            self.assertTrue(changed)
            self.assertLessEqual(len(out), cap)

    def test_trim_payload_enforces_budget_on_flat_dict_and_long_text(self) -> None:
        flat_dict = {f"key_{i}": f"value_{i}" for i in range(300)}
        res_dict = trim_payload(flat_dict, TrimConfig(max_tokens=40), counter=self.counter)
        self.assertTrue(res_dict.was_trimmed)
        self.assertLessEqual(res_dict.trimmed_tokens, 40)

        long_text = "word " * 1000
        res_text = trim_payload(long_text, TrimConfig(max_tokens=10), counter=self.counter)
        self.assertTrue(res_text.was_trimmed)
        self.assertLessEqual(res_text.trimmed_tokens, 10)

    def test_trim_payload_truncates_large_arrays_and_strips_noisy_keys(self) -> None:
        payload = {
            "records": [
                {
                    "id": i,
                    "text": f"Entry {i} " + ("payload " * 20),
                    "avatar_url": "https://example.com/a.png",
                    "unused": None,
                }
                for i in range(120)
            ]
        }
        cfg = TrimConfig(max_tokens=300, max_list_items=20, max_string_chars=120)
        res = trim_payload(payload, cfg, counter=self.counter)

        self.assertTrue(res.was_trimmed)
        self.assertLessEqual(res.trimmed_tokens, 300)
        self.assertGreater(res.saved_tokens, 0)
        self.assertIn("stripped_noisy_keys", res.actions)
        self.assertIn("dropped_nulls", res.actions)
        self.assertIn("truncated_lists", res.actions)
        records = res.payload["records"]
        self.assertTrue(records["_truncated"])
        self.assertEqual(records["total"], 120)
        self.assertLess(records["shown"], 120)

    def test_trim_mcp_call_result_enforces_envelope_budget_and_preserves_protocol(self) -> None:
        # Multi-block text respects whole-envelope budget
        multi = {
            "content": [
                {"type": "text", "text": "word " * 100},
                {"type": "text", "text": "word " * 100},
                {"type": "text", "text": "word " * 100},
            ]
        }
        trimmed_env, res = trim_mcp_call_result(
            multi,
            TrimConfig(max_tokens=40),
            counter=self.counter,
        )
        self.assertTrue(res.was_trimmed)
        self.assertIn("content", trimmed_env)
        self.assertLessEqual(res.trimmed_tokens, 40)

        # Empty content with large _meta keeps valid CallToolResult and marks unattainable
        empty_content = {"content": [], "_meta": {"note": "word " * 100}}
        untouched, res_empty = trim_mcp_call_result(
            empty_content,
            TrimConfig(max_tokens=40),
            counter=self.counter,
        )
        self.assertEqual(untouched, empty_content)
        self.assertFalse(res_empty.was_trimmed)
        self.assertIn("budget_unattainable", res_empty.actions)

        # structuredContent is preserved intact rather than corrupted
        structured = {
            "content": [{"type": "text", "text": "word " * 100}],
            "structuredContent": {"items": list(range(100))},
        }
        untouched_s, res_s = trim_mcp_call_result(
            structured,
            TrimConfig(max_tokens=40),
            counter=self.counter,
        )
        self.assertEqual(untouched_s, structured)
        self.assertFalse(res_s.was_trimmed)
        self.assertIn("budget_unattainable", res_s.actions)

        # JSON text block inside CallToolResult trims cleanly
        huge = {"items": [{"id": i, "body": "long log line " * 15} for i in range(80)]}
        envelope = {"content": [{"type": "text", "text": json.dumps(huge)}]}
        trimmed_json_env, res_json = trim_mcp_call_result(
            envelope,
            TrimConfig(max_tokens=280),
            counter=self.counter,
        )
        self.assertTrue(res_json.was_trimmed)
        self.assertLessEqual(res_json.trimmed_tokens, 280)
        parsed = json.loads(trimmed_json_env["content"][0]["text"])
        self.assertIn("items", parsed)

    def test_trim_response_decorator_sync_and_async(self) -> None:
        @trim_response(max_tokens=200, max_list_items=10)
        def sync_tool() -> dict[str, object]:
            return {
                "rows": [
                    {"idx": i, "avatar_url": "x", "val": "abc " * 20} for i in range(60)
                ]
            }

        @trim_response(max_tokens=200, max_list_items=10)
        async def async_tool() -> dict[str, object]:
            return {
                "rows": [
                    {"idx": i, "avatar_url": "x", "val": "abc " * 20} for i in range(60)
                ]
            }

        sync_out = sync_tool()
        self.assertTrue(inspect.iscoroutinefunction(async_tool))
        coro = async_tool()
        try:
            coro.send(None)
        except StopIteration as stop:
            async_out = stop.value
        else:
            self.fail("Expected async_tool coroutine to complete without awaiting I/O")

        self.assertIsInstance(sync_out["rows"], dict)
        self.assertTrue(sync_out["rows"]["_truncated"])  # type: ignore[index]
        self.assertIsInstance(async_out["rows"], dict)
        self.assertTrue(async_out["rows"]["_truncated"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
