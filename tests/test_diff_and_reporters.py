"""Unit tests for snapshot diff gating and output reporters (Terminal, Markdown, JSON, SARIF)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcp_diet.analyzer import analyze_manifest
from mcp_diet.diff_gate import build_snapshot, diff_snapshots, load_snapshot, save_snapshot
from mcp_diet.models import BudgetPolicy, ServerManifest, ToolItem
from mcp_diet.reporters import (
    render_diff_json,
    render_diff_markdown,
    render_diff_terminal,
    render_json,
    render_markdown,
    render_sarif,
    render_terminal,
)
from mcp_diet.tokenizer import HeuristicTokenCounter


class TestDiffGateAndReporters(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = HeuristicTokenCounter()
        self.base_manifest = ServerManifest(
            server_name="orders-mcp",
            source="orders.json",
            tools=[
                ToolItem.from_mcp_dict(
                    {
                        "name": "get_order",
                        "description": "Fetch an order by ID.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"order_id": {"type": "string"}},
                        },
                    }
                )
            ],
        )

    def test_snapshot_roundtrip_and_diff_gate_failure(self) -> None:
        baseline = build_snapshot(self.base_manifest, counter=self.counter)
        with tempfile.TemporaryDirectory() as tmp:
            snap_path = save_snapshot(baseline, Path(tmp) / "baseline.json")
            reloaded = load_snapshot(snap_path)

        expanded = ServerManifest(
            server_name="orders-mcp",
            source="orders.json",
            tools=[
                *self.base_manifest.tools,
                ToolItem.from_mcp_dict(
                    {
                        "name": "search_orders_everything",
                        "description": "Search every order with verbose docs. " * 25,
                        "inputSchema": {
                            "type": "object",
                            "properties": {f"p{i}": {"type": "string"} for i in range(15)},
                        },
                    }
                ),
            ],
        )

        diff = diff_snapshots(
            reloaded,
            expanded,
            max_increase=50,
            max_total=150,
            counter=self.counter,
        )
        self.assertFalse(diff.passed)
        self.assertGreater(diff.delta, 50)
        self.assertEqual(len(diff.failure_reasons), 2)
        self.assertTrue(
            any(
                td.name == "search_orders_everything" and td.status == "added"
                for td in diff.tool_diffs
            )
        )

        md = render_diff_markdown(diff)
        term = render_diff_terminal(diff, color=False)
        parsed_json = json.loads(render_diff_json(diff))
        self.assertIn("search_orders_everything", md)
        self.assertIn("FAIL", term)
        self.assertFalse(parsed_json["passed"])

    def test_reporters_produce_valid_outputs(self) -> None:
        report = analyze_manifest(
            self.base_manifest,
            BudgetPolicy(max_total_tokens=10, max_tool_tokens=5),
            counter=self.counter,
        )

        term = render_terminal(report, color=False)
        self.assertIn("orders-mcp", term)
        self.assertIn("DIET001", term)

        md = render_markdown(report)
        self.assertIn("## mcp-diet Context Budget Report", md)
        self.assertIn("`get_order`", md)

        parsed = json.loads(render_json(report))
        self.assertEqual(parsed["server_name"], "orders-mcp")
        self.assertGreater(parsed["summary"]["total_tokens"], 0)

        sarif = json.loads(render_sarif(report))
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertEqual(sarif["runs"][0]["tool"]["driver"]["name"], "mcp-diet")
        self.assertGreaterEqual(len(sarif["runs"][0]["results"]), 2)
        # Without explicit artifact_uri, physicalLocation must not emit raw commands/paths
        for res in sarif["runs"][0]["results"]:
            self.assertNotIn("physicalLocation", res["locations"][0])

        sarif_with_uri = json.loads(
            render_sarif(report, artifact_uri="fixtures/tools%20with%20space.json")
        )
        first_loc = sarif_with_uri["runs"][0]["results"][0]["locations"][0]
        self.assertEqual(
            first_loc["physicalLocation"]["artifactLocation"]["uri"],
            "fixtures/tools%20with%20space.json",
        )


if __name__ == "__main__":
    unittest.main()
