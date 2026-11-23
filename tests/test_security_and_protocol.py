"""Security, protocol, framing, SSE, session, and pagination boundary tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mcp_diet.client import (
    _collect_pages,
    _NoRedirect,
    _read_http_rpc,
    inspect_http_server,
    inspect_stdio_server,
    parse_command_string,
    read_stdio_frame,
)
from mcp_diet.models import TrimConfig
from mcp_diet.proxy import ProxyInterceptor
from mcp_diet.tokenizer import HeuristicTokenCounter

ROOT_DIR = Path(__file__).resolve().parent.parent


class _FakeHttpResponse(io.RawIOBase):
    """Minimal HTTPResponse-like streaming object supporting early return before EOF."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        content_type: str = "application/json",
        status: int = 200,
        session_id: str | None = None,
        fail_if_eof_reached: bool = False,
    ) -> None:
        self._chunks = list(chunks)
        self._buffer = b""
        self.status = status
        self.fail_if_eof_reached = fail_if_eof_reached
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if session_id is not None:
            self.headers["Mcp-Session-Id"] = session_id

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:  # type: ignore[override]
        while not self._buffer:
            if not self._chunks:
                if self.fail_if_eof_reached:
                    raise AssertionError("Parser waited for EOF after complete SSE response")
                return 0
            self._buffer = self._chunks.pop(0)
        n = min(len(b), len(self._buffer))
        b[:n] = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return n

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data = self._buffer + b"".join(self._chunks)
            self._buffer = b""
            self._chunks.clear()
            return data
        out = bytearray()
        while len(out) < size:
            if not self._buffer:
                if not self._chunks:
                    break
                self._buffer = self._chunks.pop(0)
            take = min(size - len(out), len(self._buffer))
            out.extend(self._buffer[:take])
            self._buffer = self._buffer[take:]
        return bytes(out)

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class TestSecurityAndProtocol(unittest.TestCase):
    def test_stdio_frame_boundaries_and_eof(self) -> None:
        # Chunked write of a single large frame still yields one message
        large_msg = json.dumps({"jsonrpc": "2.0", "id": 1, "data": "a" * 16384}).encode() + b"\n"

        class _ChunkedReader(io.RawIOBase):
            def __init__(self, data: bytes) -> None:
                self._pos = 0
                self._data = data

            def readable(self) -> bool:
                return True

            def readinto(self, b: bytearray | memoryview) -> int:  # type: ignore[override]
                if self._pos >= len(self._data):
                    return 0
                step = min(len(b), 127, len(self._data) - self._pos)
                b[:step] = self._data[self._pos : self._pos + step]
                self._pos += step
                return step

        buffered = io.BufferedReader(_ChunkedReader(large_msg))
        frame = read_stdio_frame(buffered, max_bytes=65536)
        self.assertEqual(frame, large_msg)
        self.assertEqual(read_stdio_frame(buffered, max_bytes=65536), b"")

        # Oversized frame raises ValueError
        with self.assertRaisesRegex(ValueError, "exceeds size limit"):
            read_stdio_frame(io.BytesIO(b"x" * 200 + b"\n"), max_bytes=100)

        # Truncated frame without trailing newline raises ValueError
        with self.assertRaisesRegex(ValueError, "ended inside a frame"):
            read_stdio_frame(io.BytesIO(b'{"jsonrpc":"2.0","id":1}'), max_bytes=1024)

    def test_stdio_inspector_rejects_oversized_frame_and_reaps_child(self) -> None:
        script = (
            "import sys; sys.stdin.readline(); "
            "sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":\"' "
            "+ 'a'*400 + '\"}\\n'); "
            "sys.stdout.flush()"
        )
        t0 = time.monotonic()
        with self.assertRaisesRegex(ValueError, "exceeds size limit"):
            inspect_stdio_server(
                [sys.executable, "-c", script],
                timeout=3.0,
                max_frame_bytes=128,
            )
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_proxy_does_not_hang_when_child_closes_stdout_but_keeps_stderr_open(self) -> None:
        child_code = (
            "import sys, time; "
            "sys.stdout.close(); "
            "sys.stderr.write('stderr still open\\n'); sys.stderr.flush(); "
            "time.sleep(30)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT_DIR / "src") + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "mcp_diet", "proxy", "--", sys.executable, "-c", child_code],
            input="",
            text=True,
            capture_output=True,
            env=env,
            timeout=8,
            check=False,
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 6.0)
        self.assertIn("stderr still open", proc.stderr)

    def test_proxy_notifications_errors_and_invalid_stdout_isolation(self) -> None:
        log = io.StringIO()
        interceptor = ProxyInterceptor(
            compact_schemas=True,
            trim_config=TrimConfig(max_tokens=100),
            counter=HeuristicTokenCounter(),
            log_stream=log,
            verbose=True,
        )

        # Notification (no id) must be forwarded verbatim and not registered in pending
        notif = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        self.assertEqual(interceptor.process_client_line(notif), notif)
        self.assertEqual(interceptor._pending_methods, {})

        # Register a request, then receive JSON-RPC error -> forward verbatim & clean pending
        req = b'{"jsonrpc":"2.0","id":42,"method":"tools/call","params":{}}\n'
        self.assertEqual(interceptor.process_client_line(req), req)
        self.assertIn(42, interceptor._pending_methods)

        err_resp = b'{"jsonrpc":"2.0","id":42,"error":{"code":-32000,"message":"boom"}}\n'
        self.assertEqual(interceptor.process_server_line(err_resp), err_resp)
        self.assertNotIn(42, interceptor._pending_methods)

        # Stray plain text on upstream stdout must be dropped, never leaked to host stdout
        self.assertEqual(interceptor.process_server_line(b"DEBUG: server booted\n"), b"")

        # Server notification is forwarded verbatim
        srv_notif = b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n'
        self.assertEqual(interceptor.process_server_line(srv_notif), srv_notif)

    def test_http_no_redirect_blocks_302_credential_leak(self) -> None:
        handler = _NoRedirect()
        req = urllib.request.Request(
            "https://origin-a.example/mcp",
            headers={"Authorization": "Bearer secret-token"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(
                req,
                io.BytesIO(b""),
                302,
                "Found",
                Message(),
                "https://evil-b.example/steal",
            )
        self.assertEqual(ctx.exception.code, 302)

    def test_sse_bom_comments_multiline_batch_and_early_return_before_eof(self) -> None:
        # UTF-8 BOM + comment + unrelated notification + multiline batch matching id=1
        chunks = [
            b"\xef\xbb\xbf: keepalive comment\n\n",
            b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n',
            b'data: [{"jsonrpc":"2.0",\n',
            b'data: "id":1,\n',
            b'data: "result":{"ok":true}}]\n',
            b"\n",
        ]
        resp = _FakeHttpResponse(
            chunks,
            content_type="text/event-stream",
            fail_if_eof_reached=True,
        )
        matched = _read_http_rpc(resp, request_id=1)  # type: ignore[arg-type]
        self.assertEqual(matched["id"], 1)
        self.assertEqual(matched["result"], {"ok": True})

        # Truncated event at EOF without terminating blank line must NOT be accepted
        truncated = _FakeHttpResponse(
            [b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'],
            content_type="text/event-stream",
        )
        with self.assertRaisesRegex(ValueError, "Missing complete MCP response"):
            _read_http_rpc(truncated, request_id=1)  # type: ignore[arg-type]

    def test_http_session_404_reinitializes_cleanly_without_mixing_pages(self) -> None:
        seen_headers: list[dict[str, str]] = []
        call_num = 0

        test_case = self

        class _FakeOpener:
            def open(
                self, req: urllib.request.Request, timeout: float = 15.0
            ) -> _FakeHttpResponse:
                nonlocal call_num
                call_num += 1
                hdrs = {k.lower(): v for k, v in req.header_items()}
                seen_headers.append(hdrs)
                payload = json.loads((req.data or b"{}").decode("utf-8"))
                method = payload.get("method")
                req_id = payload.get("id")

                if call_num == 1 and method == "initialize":
                    body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "sess-mcp", "version": "1.0"},
                            },
                        }
                    ).encode()
                    return _FakeHttpResponse([body], session_id="sess-1")

                if method == "notifications/initialized":
                    return _FakeHttpResponse([b""], status=202)

                # First session's tools/list expires with 404
                if hdrs.get("mcp-session-id") == "sess-1" and method == "tools/list":
                    raise urllib.error.HTTPError(
                        req.full_url, 404, "Not Found", Message(), io.BytesIO(b"expired")
                    )

                # Re-init must NOT carry old session ID
                if method == "initialize":
                    test_case.assertNotIn("mcp-session-id", hdrs)
                    body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "sess-mcp", "version": "1.0"},
                            },
                        }
                    ).encode()
                    return _FakeHttpResponse([body], session_id="sess-2")

                if hdrs.get("mcp-session-id") == "sess-2" and method == "tools/list":
                    body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "tools": [
                                    {
                                        "name": "fresh_tool",
                                        "description": "From session 2",
                                        "inputSchema": {"type": "object"},
                                    }
                                ]
                            },
                        }
                    ).encode()
                    return _FakeHttpResponse([body])

                raise AssertionError(f"Unexpected call #{call_num}: {method} {hdrs}")

        with patch("urllib.request.build_opener", return_value=_FakeOpener()):
            manifest = inspect_http_server(
                "https://mcp.example/endpoint",
                headers={"Mcp-Session-Id": "stale-caller-id", "Authorization": "Bearer ok"},
            )

        self.assertEqual([t.name for t in manifest.tools], ["fresh_tool"])
        self.assertNotIn("mcp-session-id", seen_headers[0])

    def test_pagination_completeness_errors(self) -> None:
        cases: list[tuple[str, Any, str]] = [
            (
                "repeated_cursor",
                lambda _m, _p: {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"tools": [{"name": "a"}], "nextCursor": "same"},
                },
                "repeated",
            ),
            (
                "exceeds_page_limit",
                lambda _m, p: {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [{"name": "a"}],
                        "nextCursor": f"page-{int(p.get('cursor', 'page-0').split('-')[1]) + 1}",
                    },
                },
                "exceeded",
            ),
            (
                "second_page_rpc_error",
                lambda _m, p: (
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "error": {"code": -32000, "message": "page 2 failed"},
                    }
                    if p.get("cursor") == "p2"
                    else {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"tools": [{"name": "a"}], "nextCursor": "p2"},
                    }
                ),
                "failed",
            ),
        ]

        for case_name, fake_call, expected_regex in cases:
            with self.subTest(case=case_name):
                with self.assertRaisesRegex((ValueError, RuntimeError), expected_regex):
                    _collect_pages(fake_call, "tools/list", "tools", max_pages=5)

    @unittest.skipUnless(sys.platform == "win32", "Windows CommandLineToArgvW rules")
    def test_windows_parse_command_string_quotes_spaces_and_backslashes(self) -> None:
        self.assertEqual(
            parse_command_string(r'tool.exe --dir="C:\Program Files\Acme"'),
            ["tool.exe", r"--dir=C:\Program Files\Acme"],
        )
        self.assertEqual(
            parse_command_string(r'"C:\Program Files\Python\python.exe" -m mcp_diet'),
            [r"C:\Program Files\Python\python.exe", "-m", "mcp_diet"],
        )
        self.assertEqual(
            parse_command_string(r'tool.exe "C:\dir\\" "say \"hi\""'),
            ["tool.exe", "C:\\dir\\", 'say "hi"'],
        )
        self.assertEqual(parse_command_string("   "), [])


if __name__ == "__main__":
    unittest.main()
