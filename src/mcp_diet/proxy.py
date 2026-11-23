"""Transparent MCP stdio slimming proxy.

Sits between an MCP client (Codex CLI, Claude Code, Cursor, etc.) and any upstream
stdio MCP server to compact bloated `tools/list` schemas and trim oversized
`tools/call` responses on the fly. All diagnostic logs go strictly to stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TextIO

from mcp_diet.client import (
    MAX_STDIO_FRAME_BYTES,
    decode_stdio_frame,
    parse_command_string,
    read_stdio_frame,
    reap_subprocess,
)
from mcp_diet.models import TrimConfig
from mcp_diet.tokenizer import TokenCounter, get_token_counter
from mcp_diet.trimmer import compact_schema, trim_mcp_call_result


@dataclass(slots=True)
class ProxyStats:
    """Cumulative token savings tracked across a proxy session."""

    schemas_compacted: int = 0
    schema_tokens_saved: int = 0
    responses_trimmed: int = 0
    response_tokens_saved: int = 0


def _is_valid_jsonrpc_message(msg: object) -> bool:
    """Validate that `msg` is a structurally sound JSON-RPC 2.0 request/notification/response."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return False
    if "method" in msg:
        if not isinstance(msg.get("method"), str) or not msg["method"]:
            return False
        if "result" in msg or "error" in msg:
            return False
        return "id" not in msg or type(msg["id"]) in (str, int)
    if "id" not in msg:
        return False
    if msg["id"] is not None and type(msg["id"]) not in (str, int):
        return False
    return ("result" in msg) != ("error" in msg)


def _validate_envelope(decoded: object) -> list[dict[str, Any]] | None:
    """Validate a single JSON-RPC object or non-empty batch; return list of dicts or None."""
    if isinstance(decoded, dict):
        return [decoded] if _is_valid_jsonrpc_message(decoded) else None
    if isinstance(decoded, list) and decoded and all(
        _is_valid_jsonrpc_message(item) for item in decoded
    ):
        return decoded
    return None


class ProxyInterceptor:
    """Thread-safe stateful JSON-RPC message interceptor for MCP schema & response slimming."""

    def __init__(
        self,
        *,
        compact_schemas: bool = True,
        trim_config: TrimConfig | None = None,
        counter: TokenCounter | None = None,
        log_stream: TextIO | None = None,
        verbose: bool = False,
    ) -> None:
        self.compact_schemas = compact_schemas
        self.trim_config = trim_config or TrimConfig()
        self.counter = counter or get_token_counter()
        self.log_stream = log_stream
        self.verbose = verbose
        self.stats = ProxyStats()
        self._pending_methods: dict[str | int, str] = {}
        self._lock = threading.Lock()

    def _log(self, message: str) -> None:
        if self.log_stream is not None and self.verbose:
            self.log_stream.write(f"[mcp-diet proxy] {message}\n")
            self.log_stream.flush()

    def process_client_line(self, raw_line: bytes) -> bytes:
        """Validate a client -> upstream JSON-RPC frame and record pending request methods."""
        if not raw_line.strip():
            return b""
        try:
            decoded = decode_stdio_frame(raw_line)
        except ValueError:
            self._log("Dropped invalid non-JSON client frame.")
            return b""

        messages = _validate_envelope(decoded)
        if messages is None:
            self._log("Dropped invalid JSON-RPC client envelope.")
            return b""

        for msg in messages:
            req_id = msg.get("id")
            method = msg.get("method")
            if (type(req_id) is str or type(req_id) is int) and isinstance(method, str):
                with self._lock:
                    self._pending_methods[req_id] = method
        return raw_line

    def _slim_response_message(self, msg: dict[str, Any]) -> bool:
        """Apply optional schema/response slimming to a validated server response in-place."""
        req_id = msg.get("id")
        if (
            (type(req_id) is not str and type(req_id) is not int)
            or "method" in msg
            or (("result" in msg) == ("error" in msg))
        ):
            return False

        with self._lock:
            method = self._pending_methods.pop(req_id, None)

        if "error" in msg:
            return False

        result = msg.get("result")
        if not isinstance(result, dict):
            return False

        if (
            method == "tools/list"
            and self.compact_schemas
            and isinstance(result.get("tools"), list)
        ):
            before_tokens = self.counter.count_json(result["tools"])
            compacted = [
                compact_schema(t) if isinstance(t, dict) else t for t in result["tools"]
            ]
            after_tokens = self.counter.count_json(compacted)
            if after_tokens < before_tokens:
                msg["result"] = dict(result)
                msg["result"]["tools"] = compacted
                saved = before_tokens - after_tokens
                self.stats.schemas_compacted += len(compacted)
                self.stats.schema_tokens_saved += saved
                self._log(
                    f"Compacted tools/list ({len(compacted)} tools): "
                    f"{before_tokens} -> {after_tokens} tokens (saved {saved})."
                )
                return True

        elif method == "tools/call" and self.trim_config.max_tokens > 0:
            new_result, trim_res = trim_mcp_call_result(
                result,
                self.trim_config,
                counter=self.counter,
            )
            if trim_res.was_trimmed:
                msg["result"] = new_result
                self.stats.responses_trimmed += 1
                self.stats.response_tokens_saved += trim_res.saved_tokens
                self._log(
                    f"Trimmed tools/call response: {trim_res.original_tokens} -> "
                    f"{trim_res.trimmed_tokens} tokens (saved {trim_res.saved_tokens}, "
                    f"actions={','.join(trim_res.actions)})."
                )
                return True

        return False

    def process_server_line(self, raw_line: bytes) -> bytes:
        """Validate an upstream -> client JSON-RPC frame and safely apply configured slimming."""
        if not raw_line.strip():
            return b""
        try:
            decoded = decode_stdio_frame(raw_line)
        except ValueError:
            self._log("Dropped invalid non-JSON upstream stdout line.")
            return b""

        messages = _validate_envelope(decoded)
        if messages is None:
            self._log("Dropped invalid JSON-RPC upstream envelope.")
            return b""

        try:
            modified = False
            for msg in messages:
                if self._slim_response_message(msg):
                    modified = True
            if not modified:
                return raw_line
            out_obj: Any = decoded
            encoded = json.dumps(
                out_obj,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            return (encoded + "\n").encode("utf-8")
        except Exception as exc:
            self._log(f"Slimming failed ({type(exc).__name__}); forwarding original frame.")
            return raw_line


def run_stdio_proxy(
    upstream_cmd: str | list[str],
    *,
    compact_schemas: bool = True,
    trim_config: TrimConfig | None = None,
    verbose: bool = False,
    max_frame_bytes: int = MAX_STDIO_FRAME_BYTES,
    drain_timeout: float = 5.0,
) -> int:
    """Spawn `upstream_cmd` and transparently proxy JSON-RPC lines over stdin/stdout."""
    argv = parse_command_string(upstream_cmd)
    if not argv:
        raise ValueError("Upstream MCP command is required for proxy mode.")

    interceptor = ProxyInterceptor(
        compact_schemas=compact_schemas,
        trim_config=trim_config,
        log_stream=sys.stderr,
        verbose=verbose,
    )

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    input_done = threading.Event()
    output_done = threading.Event()
    failed = threading.Event()

    def pump_stdin() -> None:
        assert proc.stdin is not None
        try:
            with proc.stdin as stdin_pipe:
                while not failed.is_set():
                    line = read_stdio_frame(sys.stdin.buffer, max_bytes=max_frame_bytes)
                    if not line:
                        break
                    out_line = interceptor.process_client_line(line)
                    if out_line:
                        stdin_pipe.write(out_line)
                        stdin_pipe.flush()
        except Exception as exc:
            failed.set()
            if verbose:
                sys.stderr.write(f"[mcp-diet proxy] stdin error: {exc}\n")
                sys.stderr.flush()
        finally:
            input_done.set()

    def pump_stdout() -> None:
        assert proc.stdout is not None
        try:
            with proc.stdout as stdout_pipe:
                while not failed.is_set():
                    line = read_stdio_frame(stdout_pipe, max_bytes=max_frame_bytes)
                    if not line:
                        break
                    out_line = interceptor.process_server_line(line)
                    if out_line:
                        sys.stdout.buffer.write(out_line)
                        sys.stdout.buffer.flush()
        except Exception as exc:
            failed.set()
            if verbose:
                sys.stderr.write(f"[mcp-diet proxy] stdout error: {exc}\n")
                sys.stderr.flush()
        finally:
            output_done.set()

    def pump_stderr() -> None:
        assert proc.stderr is not None
        try:
            with proc.stderr as stderr_pipe:
                while True:
                    chunk = stderr_pipe.read(4096)
                    if not chunk:
                        break
                    sys.stderr.buffer.write(chunk)
                    sys.stderr.buffer.flush()
        except OSError:
            pass

    t_in = threading.Thread(target=pump_stdin, daemon=True)
    t_out = threading.Thread(target=pump_stdout, daemon=True)
    t_err = threading.Thread(target=pump_stderr, daemon=True)

    t_in.start()
    t_out.start()
    t_err.start()

    deadline: float | None = None
    try:
        while proc.poll() is None and not output_done.wait(0.05):
            if failed.is_set():
                break
            if input_done.is_set() and deadline is None:
                deadline = time.monotonic() + drain_timeout
            if deadline is not None and time.monotonic() >= deadline:
                break
        if output_done.is_set() and not failed.is_set() and proc.poll() is None:
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=0.25)
    finally:
        try:
            reap_subprocess(proc)
        finally:
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)

    if failed.is_set():
        return 1
    return proc.returncode if proc.returncode is not None else 1
