"""End-to-end tests for live stdio MCP inspection, proxy, config discovery, and CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp_diet.cli import main as cli_main
from mcp_diet.client import discover_config_servers, inspect_stdio_server
from mcp_diet.models import TrimConfig
from mcp_diet.proxy import ProxyInterceptor
from mcp_diet.tokenizer import HeuristicTokenCounter

ROOT_DIR = Path(__file__).resolve().parent.parent
MOCK_SERVER = ROOT_DIR / "examples" / "mock_mcp_server.py"
BLOATED_FILE = ROOT_DIR / "examples" / "bloated_tools.json"


class TestE2EClientProxyCLI(unittest.TestCase):
    def test_live_stdio_inspection_against_mock_server(self) -> None:
        manifest = inspect_stdio_server([sys.executable, str(MOCK_SERVER)])
        self.assertEqual(manifest.server_name, "mock-bloated-mcp")
        tool_names = {t.name for t in manifest.tools}
        self.assertIn("query_analytics_warehouse", tool_names)
        self.assertIn("fetch_huge_logs", tool_names)

    def test_live_stdio_proxy_compacts_schemas_and_trims_huge_tool_call(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT_DIR / "src") + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )

        proxy_cmd = [
            sys.executable,
            "-m",
            "mcp_diet",
            "proxy",
            "--compact-schemas",
            "--max-response-tokens",
            "350",
            "--verbose",
            "--",
            sys.executable,
            str(MOCK_SERVER),
        ]

        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test-host", "version": "1.0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "fetch_huge_logs", "arguments": {"count": 120}},
            },
        ]
        stdin_blob = "\n".join(json.dumps(r) for r in requests) + "\n"

        proc = subprocess.run(
            proxy_cmd,
            input=stdin_blob,
            text=True,
            capture_output=True,
            env=env,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr was: {proc.stderr}")

        responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        by_id = {r["id"]: r for r in responses if "id" in r}

        tools = by_id[2]["result"]["tools"]
        query_tool = next(t for t in tools if t["name"] == "query_analytics_warehouse")
        self.assertNotIn("title", query_tool["inputSchema"])

        call_text = by_id[3]["result"]["content"][0]["text"]
        parsed_call = json.loads(call_text)
        self.assertTrue(parsed_call["records"]["_truncated"])
        self.assertEqual(parsed_call["records"]["total"], 120)
        self.assertLess(parsed_call["records"]["shown"], 120)
        self.assertIn("[mcp-diet proxy]", proc.stderr)

    def test_proxy_interceptor_unit(self) -> None:
        buf = io.StringIO()
        interceptor = ProxyInterceptor(
            compact_schemas=True,
            trim_config=TrimConfig(max_tokens=200),
            counter=HeuristicTokenCounter(),
            log_stream=buf,
            verbose=True,
        )
        req = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).encode() + b"\n"
        self.assertEqual(interceptor.process_client_line(req), req)
        resp_obj = {
            "jsonrpc": "2.0",
            "id": 9,
            "result": {
                "tools": [
                    {
                        "name": "t1",
                        "description": "Long guide " * 50,
                        "inputSchema": {
                            "type": "object",
                            "title": "T1Input",
                            "properties": {},
                        },
                    }
                ]
            },
        }
        resp = json.dumps(resp_obj).encode() + b"\n"
        out = json.loads(interceptor.process_server_line(resp).decode())
        self.assertNotIn("title", out["result"]["tools"][0]["inputSchema"])
        self.assertGreater(interceptor.stats.schema_tokens_saved, 0)

    def test_discover_config_servers_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_cfg = Path(tmp) / "mcp.json"
            json_cfg.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "local": {"command": "python", "args": ["server.py"]},
                            "remote": {"url": "https://mcp.example.com/mcp"},
                            "off": {"command": "node", "args": ["x.js"], "disabled": True},
                        }
                    }
                ),
                encoding="utf-8",
            )
            found = discover_config_servers(json_cfg)
            self.assertEqual({s.name for s in found}, {"local", "remote"})

    def test_discover_config_servers_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toml_cfg = Path(tmp) / "config.toml"
            toml_cfg.write_text(
                '[mcp_servers.codex_local]\ncommand = "uvx"\nargs = ["my-server"]\n\n'
                '[mcp_servers.codex_http]\nurl = "http://localhost:9000/mcp"\n',
                encoding="utf-8",
            )
            if sys.version_info < (3, 11):
                with self.assertRaisesRegex(RuntimeError, r"requires Python 3\.11\+"):
                    discover_config_servers(toml_cfg)
            else:
                found_toml = discover_config_servers(toml_cfg)
                self.assertEqual({s.name for s in found_toml}, {"codex_local", "codex_http"})

    def test_cli_demo_check_snapshot_and_diff(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = cli_main(["demo"])
        self.assertEqual(rc, 0)
        self.assertIn("demo-github-mcp", stdout.getvalue())

        stdout_check = io.StringIO()
        with contextlib.redirect_stdout(stdout_check):
            rc_fail = cli_main(
                ["check", "--file", str(BLOATED_FILE), "--max-total", "200", "--fail-on", "error"]
            )
        self.assertEqual(rc_fail, 1)

        with tempfile.TemporaryDirectory() as tmp:
            snap_file = Path(tmp) / "snap.json"
            rc_snap = cli_main(["snapshot", "--file", str(BLOATED_FILE), "-o", str(snap_file)])
            self.assertEqual(rc_snap, 0)
            stdout_diff = io.StringIO()
            with contextlib.redirect_stdout(stdout_diff):
                rc_diff = cli_main(
                    [
                        "diff",
                        "--baseline",
                        str(snap_file),
                        "--file",
                        str(BLOATED_FILE),
                        "--max-increase",
                        "0",
                        "--format",
                        "markdown",
                    ]
                )
            self.assertEqual(rc_diff, 0)
            self.assertIn("PASS", stdout_diff.getvalue())

    def test_cli_sarif_uri_handling_for_file_with_spaces_and_live_cmd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spaced = Path(tmp) / "tools with space.json"
            spaced.write_text(BLOATED_FILE.read_text(encoding="utf-8"), encoding="utf-8")

            stdout_file = io.StringIO()
            with contextlib.redirect_stdout(stdout_file):
                rc = cli_main(
                    ["inspect", "--file", str(spaced), "--max-total", "200", "--format", "sarif"]
                )
            self.assertEqual(rc, 0)
            sarif_file = json.loads(stdout_file.getvalue())
            loc = sarif_file["runs"][0]["results"][0]["locations"][0]
            uri = loc["physicalLocation"]["artifactLocation"]["uri"]
            self.assertNotIn(" ", uri)
            self.assertNotIn("\\", uri)
            self.assertTrue(uri.startswith("file:///") or "%20" in uri)

        stdout_live = io.StringIO()
        with contextlib.redirect_stdout(stdout_live):
            rc_live = cli_main(["demo", "--format", "sarif"])
        self.assertEqual(rc_live, 0)
        sarif_live = json.loads(stdout_live.getvalue())
        for res in sarif_live["runs"][0]["results"]:
            self.assertNotIn("physicalLocation", res["locations"][0])


if __name__ == "__main__":
    unittest.main()
