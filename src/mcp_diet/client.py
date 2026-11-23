"""Zero-dependency MCP JSON-RPC 2.0 client (stdio, Streamable HTTP, offline files, configs)."""

from __future__ import annotations

import io
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from http.client import HTTPMessage, HTTPResponse
from pathlib import Path
from typing import IO, Any, NoReturn

from mcp_diet.models import PromptItem, ResourceItem, ServerManifest, ToolItem

MCP_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
)
MAX_STDIO_FRAME_BYTES = 8 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024
MAX_PAGINATION_PAGES = 50


class _McpHttpError(RuntimeError):
    """HTTP error carrying status code without reading unbounded error bodies."""

    def __init__(self, status: int) -> None:
        super().__init__(f"MCP endpoint returned HTTP {status}")
        self.status = status


class _SessionExpired(RuntimeError):
    """Raised when an HTTP MCP session returns 404 and requires re-initialization."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reject HTTP redirects so credentials and session headers never leak cross-origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> NoReturn:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


@dataclass(slots=True)
class DiscoveredServer:
    """An MCP server entry parsed from a client configuration file."""

    name: str
    transport: str  # "stdio" | "http"
    command: list[str] = field(default_factory=list)
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    source: str = ""


def parse_command_string(cmd: str | list[str]) -> list[str]:
    """Split a command string into argv tokens accurately across Windows and POSIX."""
    if isinstance(cmd, list):
        return [str(part) for part in cmd]
    if sys.platform != "win32":
        return shlex.split(cmd)

    import ctypes
    from ctypes import wintypes

    stripped = cmd.lstrip(" \t")
    if not stripped:
        return []

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    split_fn = shell32.CommandLineToArgvW
    split_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    split_fn.restype = ctypes.POINTER(wintypes.LPWSTR)

    free_fn = kernel32.LocalFree
    free_fn.argtypes = [ctypes.c_void_p]
    free_fn.restype = ctypes.c_void_p

    argc = ctypes.c_int()
    argv = split_fn(stripped, ctypes.byref(argc))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [str(argv[i]) for i in range(argc.value)]
    finally:
        free_fn(argv)


def read_stdio_frame(stream: IO[bytes], max_bytes: int = MAX_STDIO_FRAME_BYTES) -> bytes:
    """Read one newline-delimited JSON-RPC stdio frame with strict size and EOF checks."""
    line = stream.readline(max_bytes + 1)
    if len(line) > max_bytes:
        raise ValueError("MCP stdio frame exceeds size limit")
    if line and not line.endswith(b"\n"):
        raise ValueError("MCP stdio ended inside a frame")
    return line


def decode_stdio_frame(line: bytes) -> object:
    """Strictly decode a UTF-8 JSON frame, surfacing malformed UTF-8, JSON, or recursion."""
    try:
        return json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise ValueError("Invalid MCP JSON frame") from exc


def match_jsonrpc_response(value: object, request_id: int) -> dict[str, Any] | None:
    """Return the JSON-RPC 2.0 response matching `request_id` from a message or batch."""
    messages = value if isinstance(value, list) else [value]
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("jsonrpc") == "2.0"
            and type(message.get("id")) is int
            and message["id"] == request_id
            and "method" not in message
            and (("result" in message) != ("error" in message))
        ):
            return message
    return None


def reap_subprocess(proc: subprocess.Popen[bytes]) -> None:
    """Terminate and reap a direct Popen child with bounded timeouts."""
    if proc.poll() is not None:
        return
    with suppress(ProcessLookupError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError, OSError):
            proc.kill()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)


def _validate_initialize_result(init_resp: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate InitializeResult envelope and negotiated protocolVersion."""
    if "error" in init_resp:
        raise RuntimeError(f"MCP initialize failed: {init_resp['error']}")
    init_result = init_resp.get("result")
    if not isinstance(init_result, dict):
        raise ValueError("Invalid MCP initialize result")
    protocol_version = init_result.get("protocolVersion")
    if not isinstance(protocol_version, str) or protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise RuntimeError(f"Unsupported MCP protocolVersion: {protocol_version!r}")
    return init_result, protocol_version


def _collect_pages(
    call: Callable[[str, dict[str, Any]], dict[str, Any]],
    method: str,
    key: str,
    max_pages: int = MAX_PAGINATION_PAGES,
) -> list[dict[str, Any]]:
    """Collect paginated MCP list responses, failing loudly on errors or cursor loops."""
    items: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None

    for _ in range(max_pages):
        params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
        reply = call(method, params)
        result = reply.get("result")
        if "error" in reply or not isinstance(result, dict):
            raise RuntimeError(f"{method} failed: {reply.get('error', 'invalid result')}")
        batch = result.get(key)
        if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
            raise ValueError(f"Invalid {key} page returned by {method}")
        items.extend(batch)
        if "nextCursor" not in result or result["nextCursor"] is None:
            return items
        next_cursor = result["nextCursor"]
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise ValueError(f"Invalid or repeated {method} cursor: {next_cursor!r}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    raise RuntimeError(f"{method} exceeded {max_pages} pages")


def load_manifest_from_file(path: str | Path) -> ServerManifest:
    """Load an offline MCP tools manifest JSON file.

    Accepts:
    - `{"server_name": "...", "tools": [...], "resources": [...], "prompts": [...]}`
    - JSON-RPC envelope `{"result": {"tools": [...]}}`
    - Bare JSON list of tool dicts `[...]`
    """
    file_path = Path(path)
    raw = json.loads(file_path.read_text(encoding="utf-8"))

    server_name = file_path.stem
    server_version = ""
    tools_raw: list[dict[str, Any]] = []
    resources_raw: list[dict[str, Any]] = []
    prompts_raw: list[dict[str, Any]] = []

    if isinstance(raw, list):
        tools_raw = [item for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict):
        result = raw.get("result")
        body = result if isinstance(result, dict) else raw
        info = body.get("serverInfo")
        server_info = info if isinstance(info, dict) else {}
        server_name = str(
            body.get("server_name")
            or body.get("name")
            or server_info.get("name")
            or file_path.stem
        )
        server_version = str(body.get("server_version") or server_info.get("version") or "")
        if isinstance(body.get("tools"), list):
            tools_raw = [item for item in body["tools"] if isinstance(item, dict)]
        if isinstance(body.get("resources"), list):
            resources_raw = [item for item in body["resources"] if isinstance(item, dict)]
        if isinstance(body.get("prompts"), list):
            prompts_raw = [item for item in body["prompts"] if isinstance(item, dict)]
    else:
        raise ValueError(f"Unsupported manifest structure in {file_path}")

    return ServerManifest(
        server_name=server_name,
        source=str(file_path),
        server_version=server_version,
        tools=[ToolItem.from_mcp_dict(t) for t in tools_raw],
        resources=[ResourceItem.from_mcp_dict(r) for r in resources_raw],
        prompts=[PromptItem.from_mcp_dict(p) for p in prompts_raw],
    )


def _load_toml_config(text: str) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(text)
    raise RuntimeError("Parsing TOML MCP configs requires Python 3.11+.")


def discover_config_servers(config_path: str | Path) -> list[DiscoveredServer]:
    """Parse MCP server definitions from JSON (`mcp.json`) or TOML (Codex `config.toml`)."""
    path = Path(config_path)
    text = path.read_text(encoding="utf-8")
    discovered: list[DiscoveredServer] = []

    if path.suffix.lower() == ".toml":
        servers_table = _load_toml_config(text).get("mcp_servers")
        if isinstance(servers_table, dict):
            for name, cfg in servers_table.items():
                if not isinstance(cfg, dict):
                    continue
                if "url" in cfg:
                    discovered.append(
                        DiscoveredServer(
                            name=str(name),
                            transport="http",
                            url=str(cfg["url"]),
                            source=str(path),
                        )
                    )
                elif "command" in cfg:
                    cmd = [str(cfg["command"]), *[str(a) for a in cfg.get("args", [])]]
                    env = {str(k): str(v) for k, v in dict(cfg.get("env", {})).items()}
                    discovered.append(
                        DiscoveredServer(
                            name=str(name),
                            transport="stdio",
                            command=cmd,
                            env=env,
                            source=str(path),
                        )
                    )
        return discovered

    data = json.loads(text)
    if not isinstance(data, dict):
        return discovered
    servers_obj = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers_obj, dict):
        return discovered

    for name, cfg in servers_obj.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("disabled") is True:
            continue
        if "url" in cfg or "serverUrl" in cfg:
            url = str(cfg.get("url") or cfg.get("serverUrl"))
            discovered.append(
                DiscoveredServer(name=str(name), transport="http", url=url, source=str(path))
            )
        elif "command" in cfg:
            raw_args = cfg.get("args", [])
            args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
            cmd = [str(cfg["command"]), *args]
            raw_env = cfg.get("env", {})
            env = (
                {str(k): str(v) for k, v in raw_env.items()}
                if isinstance(raw_env, dict)
                else {}
            )
            discovered.append(
                DiscoveredServer(
                    name=str(name),
                    transport="stdio",
                    command=cmd,
                    env=env,
                    source=str(path),
                )
            )
    return discovered


def inspect_stdio_server(
    cmd: str | list[str],
    *,
    server_name: str | None = None,
    timeout: float = 15.0,
    env: dict[str, str] | None = None,
    max_frame_bytes: int = MAX_STDIO_FRAME_BYTES,
) -> ServerManifest:
    """Launch a stdio MCP server, run MCP handshake, and collect tools/resources/prompts."""
    argv = parse_command_string(cmd)
    if not argv:
        raise ValueError("MCP server command cannot be empty.")

    proc_env = dict(os.environ)
    if env:
        proc_env.update(env)

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=proc_env,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    stop_event = threading.Event()
    stdout_queue: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=8)
    writes: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
    write_results: queue.Queue[Exception | None] = queue.Queue(maxsize=1)
    stderr_tail: list[bytes] = []

    def _enqueue_stdout(item: bytes | Exception | None) -> None:
        while not stop_event.is_set():
            try:
                stdout_queue.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def _write_stdin() -> None:
        assert proc.stdin is not None
        with proc.stdin as stdin_stream:
            while not stop_event.is_set():
                try:
                    payload = writes.get(timeout=0.05)
                except queue.Empty:
                    continue
                if payload is None:
                    break
                err: Exception | None = None
                try:
                    stdin_stream.write(payload)
                    stdin_stream.flush()
                except Exception as exc:
                    err = exc
                with suppress(queue.Full):
                    write_results.put(err, timeout=0.2)
                if err is not None:
                    break

    def _read_stdout() -> None:
        assert proc.stdout is not None
        with proc.stdout as stdout_stream:
            try:
                while not stop_event.is_set():
                    line = read_stdio_frame(stdout_stream, max_bytes=max_frame_bytes)
                    if not line:
                        break
                    _enqueue_stdout(line)
            except Exception as exc:
                _enqueue_stdout(exc)
            finally:
                _enqueue_stdout(None)

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        with proc.stderr as stderr_stream:
            while not stop_event.is_set():
                try:
                    chunk = stderr_stream.read(4096)
                except OSError:
                    break
                if not chunk:
                    break
                stderr_tail.append(chunk)
                if len(stderr_tail) > 8:
                    stderr_tail.pop(0)

    t_in = threading.Thread(target=_write_stdin, daemon=True)
    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_in.start()
    t_out.start()
    t_err.start()

    def _send(payload: dict[str, Any], deadline: float) -> None:
        encoded = (
            json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            writes.put(encoded, timeout=max(0.0, deadline - time.monotonic()))
            write_error = write_results.get(timeout=max(0.0, deadline - time.monotonic()))
        except (queue.Full, queue.Empty) as exc:
            raise TimeoutError("Timed out writing MCP request") from exc
        if write_error is not None:
            raise write_error

    def _rpc(req_id: int, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        _send(payload, deadline=deadline)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                err_hint = b"".join(stderr_tail).decode("utf-8", errors="replace").strip()
                extra = f" | stderr: {err_hint[:300]}" if err_hint else ""
                raise TimeoutError(f"Timed out waiting for MCP '{method}' response{extra}")
            try:
                item = stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for MCP '{method}' response") from exc

            if isinstance(item, Exception):
                raise item
            if item is None:
                err_hint = b"".join(stderr_tail).decode("utf-8", errors="replace").strip()
                extra = f" (stderr: {err_hint[:300]})" if err_hint else ""
                raise EOFError(f"MCP server exited before responding to '{method}'{extra}")

            try:
                decoded = decode_stdio_frame(item)
            except ValueError:
                continue
            match = match_jsonrpc_response(decoded, req_id)
            if match is not None:
                return match

    try:
        req_id = 1
        init_resp = _rpc(
            req_id,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-diet", "version": "0.1.0"},
            },
        )
        init_result, protocol_version = _validate_initialize_result(init_resp)
        server_info = init_result.get("serverInfo", {})
        if not isinstance(server_info, dict):
            server_info = {}
        capabilities = init_result.get("capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}

        resolved_name = str(server_name or server_info.get("name") or Path(argv[0]).name)
        server_version = str(server_info.get("version") or "")

        _send(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            deadline=time.monotonic() + timeout,
        )

        def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal req_id
            req_id += 1
            return _rpc(req_id, method, params)

        tools_raw = (
            _collect_pages(_call, "tools/list", "tools") if "tools" in capabilities else []
        )
        resources_raw = (
            _collect_pages(_call, "resources/list", "resources")
            if "resources" in capabilities
            else []
        )
        prompts_raw = (
            _collect_pages(_call, "prompts/list", "prompts") if "prompts" in capabilities else []
        )

        return ServerManifest(
            server_name=resolved_name,
            source=" ".join(argv),
            server_version=server_version,
            protocol_version=protocol_version,
            tools=[ToolItem.from_mcp_dict(t) for t in tools_raw],
            resources=[ResourceItem.from_mcp_dict(r) for r in resources_raw],
            prompts=[PromptItem.from_mcp_dict(p) for p in prompts_raw],
        )
    finally:
        stop_event.set()
        with suppress(queue.Full):
            writes.put_nowait(None)
        reap_subprocess(proc)
        t_in.join(timeout=1.0)
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)


def _read_http_rpc(
    resp: HTTPResponse,
    request_id: int,
    max_body_bytes: int = MAX_HTTP_BODY_BYTES,
) -> dict[str, Any]:
    """Read an MCP JSON-RPC response from either application/json or text/event-stream."""
    media_type = resp.headers.get_content_type()
    if media_type == "application/json":
        raw = resp.read(max_body_bytes + 1)
        if len(raw) > max_body_bytes:
            raise ValueError("MCP HTTP response exceeds size limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            raise ValueError("Invalid MCP HTTP JSON response") from exc
        match = match_jsonrpc_response(decoded, request_id)
        if match is not None:
            return match
        raise ValueError(f"Missing complete MCP response for request {request_id}")

    if media_type == "text/event-stream":
        remaining = max_body_bytes
        fields: list[str] = []
        with io.TextIOWrapper(resp, encoding="utf-8-sig", newline=None) as stream:
            while line := stream.readline(remaining + 1):
                remaining -= len(line)
                if remaining < 0:
                    raise ValueError("MCP SSE stream exceeds size limit")
                if line == "\n":
                    if fields and any(fields):
                        try:
                            decoded = json.loads("\n".join(fields))
                        except (ValueError, RecursionError) as exc:
                            raise ValueError("Invalid MCP SSE JSON event") from exc
                        match = match_jsonrpc_response(decoded, request_id)
                        if match is not None:
                            return match
                    fields.clear()
                else:
                    stripped_line = line.removesuffix("\n")
                    if stripped_line.startswith(":"):
                        continue
                    name, _, value = stripped_line.partition(":")
                    if name == "data":
                        fields.append(value.removeprefix(" "))
        raise ValueError(f"Missing complete MCP response for request {request_id}")

    raise ValueError(f"Unexpected MCP response media type: {media_type}")


def inspect_http_server(
    url: str,
    *,
    server_name: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> ServerManifest:
    """Inspect a Streamable HTTP MCP endpoint over JSON-RPC POST without following redirects."""
    base_headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        for key, value in headers.items():
            if key.lower() == "mcp-session-id":
                continue
            base_headers[key] = value

    opener = urllib.request.build_opener(_NoRedirect())

    def _inspect_once() -> ServerManifest:
        session_id: str | None = None
        req_id = 0

        def _post(payload: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal session_id
            req_headers = dict(base_headers)
            if session_id is not None:
                req_headers["Mcp-Session-Id"] = session_id
            data = json.dumps(
                payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
            try:
                with opener.open(req, timeout=timeout) as resp:
                    status = int(getattr(resp, "status", 200))
                    if "id" not in payload:
                        if status not in (200, 202, 204):
                            raise _McpHttpError(status)
                        return None
                    response = _read_http_rpc(resp, int(payload["id"]))
                    if payload.get("method") == "initialize" and "result" in response:
                        sid = resp.headers.get("Mcp-Session-Id")
                        if sid is not None:
                            if not sid or not all("!" <= ch <= "~" for ch in sid):
                                raise ValueError("Invalid Mcp-Session-Id header")
                            session_id = sid
                    return response
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status == 404 and session_id is not None:
                    raise _SessionExpired("MCP session expired") from None
                raise _McpHttpError(status) from None

        def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal req_id
            req_id += 1
            resp = _post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            if resp is None:
                raise RuntimeError(f"Empty response for MCP method '{method}'")
            return resp

        init_resp = _call(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-diet", "version": "0.1.0"},
            },
        )
        init_result, protocol_version = _validate_initialize_result(init_resp)
        server_info = init_result.get("serverInfo", {})
        if not isinstance(server_info, dict):
            server_info = {}
        capabilities = init_result.get("capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}

        resolved_name = str(server_name or server_info.get("name") or url)
        server_version = str(server_info.get("version") or "")

        _post({"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools_raw = (
            _collect_pages(_call, "tools/list", "tools") if "tools" in capabilities else []
        )
        resources_raw = (
            _collect_pages(_call, "resources/list", "resources")
            if "resources" in capabilities
            else []
        )
        prompts_raw = (
            _collect_pages(_call, "prompts/list", "prompts") if "prompts" in capabilities else []
        )

        return ServerManifest(
            server_name=resolved_name,
            source=url,
            server_version=server_version,
            protocol_version=protocol_version,
            tools=[ToolItem.from_mcp_dict(t) for t in tools_raw],
            resources=[ResourceItem.from_mcp_dict(r) for r in resources_raw],
            prompts=[PromptItem.from_mcp_dict(p) for p in prompts_raw],
        )

    try:
        return _inspect_once()
    except _SessionExpired:
        return _inspect_once()
