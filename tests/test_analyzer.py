"""Unit tests for token counting and DIET001–DIET007 analyzer rules."""

from __future__ import annotations

import unittest
from pathlib import Path

from mcp_diet.analyzer import analyze_manifest
from mcp_diet.client import load_manifest_from_file
from mcp_diet.models import BudgetPolicy, ServerManifest, Severity, ToolItem
from mcp_diet.tokenizer import HeuristicTokenCounter

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


class TestAnalyzer(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = HeuristicTokenCounter()

    def test_heuristic_tokenizer_basics(self) -> None:
        self.assertEqual(self.counter.count_text(""), 0)
        self.assertGreater(self.counter.count_text("hello world"), 0)
        small = self.counter.count_json({"name": "ping"})
        large = self.counter.count_json({"name": "ping", "items": list(range(100))})
        self.assertGreater(large, small * 10)

    def test_bloated_example_triggers_expected_rules(self) -> None:
        manifest = load_manifest_from_file(EXAMPLES_DIR / "bloated_tools.json")
        policy = BudgetPolicy(
            max_total_tokens=400,
            max_tool_tokens=200,
            max_description_tokens=60,
            max_schema_depth=3,
            max_enum_items=20,
            max_parameters=12,
        )
        report = analyze_manifest(manifest, policy, counter=self.counter)

        rule_ids = {f.rule_id for f in report.findings}
        self.assertIn("DIET001", rule_ids)  # oversized tool
        self.assertIn("DIET002", rule_ids)  # verbose description
        self.assertIn("DIET003", rule_ids)  # deep nesting (> 3)
        self.assertIn("DIET004", rule_ids)  # enum > 20 items
        self.assertIn("DIET005", rule_ids)  # duplicate shared filter sub-schema
        self.assertIn("DIET006", rule_ids)  # total menu budget exceeded
        self.assertIn("DIET007", rule_ids)  # > 12 top-level parameters
        self.assertFalse(report.passes(Severity.ERROR))
        self.assertGreater(report.potential_savings, 0)

    def test_clean_manifest_passes_with_zero_findings(self) -> None:
        manifest = ServerManifest(
            server_name="clean-mcp",
            source="memory://clean",
            tools=[
                ToolItem.from_mcp_dict(
                    {
                        "name": "get_status",
                        "description": "Return current service health status.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"verbose": {"type": "boolean"}},
                        },
                    }
                )
            ],
        )
        report = analyze_manifest(manifest, BudgetPolicy(), counter=self.counter)
        self.assertEqual(report.findings, [])
        self.assertTrue(report.passes(Severity.ERROR))
        self.assertTrue(report.passes(Severity.WARNING))
        self.assertEqual(report.tool_count, 1)

    def test_empty_description_triggers_info_diet007(self) -> None:
        manifest = ServerManifest(
            server_name="undoc-mcp",
            source="memory://undoc",
            tools=[
                ToolItem.from_mcp_dict(
                    {"name": "mystery_tool", "inputSchema": {"type": "object"}}
                )
            ],
        )
        report = analyze_manifest(manifest, BudgetPolicy(), counter=self.counter)
        self.assertTrue(
            any(f.rule_id == "DIET007" and f.severity == Severity.INFO for f in report.findings)
        )
        self.assertTrue(report.passes(Severity.WARNING))
        self.assertFalse(report.passes(Severity.INFO))


if __name__ == "__main__":
    unittest.main()
