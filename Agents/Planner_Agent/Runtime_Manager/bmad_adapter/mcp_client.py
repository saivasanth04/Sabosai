"""Client for GoModel's MCP gateway (streamable HTTP, JSON-RPC 2.0).

GoModel aggregates the ``mcp.servers`` in your config.yaml behind one endpoint:
  * ``/mcp``           every server, tools namespaced ``{slug}_{tool}`` (``duckduckgo_search``)
  * ``/mcp/{slug}``    one server, original tool names
  * ``X-MCP-Servers``  header to narrow ``/mcp`` to a subset
  * ``X-MCP-Tool-Discovery: search`` → only ``search_tools`` + ``call_tool`` are listed

Handles ``Mcp-Session-Id``, JSON or SSE responses, pagination and re-initialisation
when the gateway drops a session.
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .errors import MCPError
from .settings import GoModelSettings

PROTOCOL_VERSION = "2025-03-26"


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]

    @property
    def server(self) -> str:
        return self.name.split("_", 1)[0] if "_" in self.name else ""


class GoModelMCPClient:
    def __init__(self, settings: GoModelSettings) -> None:
        self.settings = settings
        self.url = settings.base_url.rstrip("/") + "/" + settings.mcp_path.strip("/")
        self.session_id: str | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._initialized = False

    # ----------------------------------------------------------------- public
    def initialize(self) -> dict[str, Any]:
        with self._lock:
            self.session_id = None
            result = self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ntg-planner-bmad-adapter", "version": "1.0"},
            }, allow_reinit=False)
            self._notify("notifications/initialized")
            self._initialized = True
            return result

    def list_tools(self) -> list[MCPTool]:
        self._ensure()
        tools: list[MCPTool] = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            res = self._rpc("tools/list", params)
            for t in res.get("tools", []):
                tools.append(MCPTool(t["name"], t.get("description", ""), t.get("inputSchema") or {"type": "object"},
                                     t.get("annotations") or {}))
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Return (text, is_error)."""
        self._ensure()
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        chunks: list[str] = []
        for part in res.get("content", []):
            kind = part.get("type")
            if kind == "text":
                chunks.append(part.get("text", ""))
            elif kind == "resource":
                r = part.get("resource", {})
                chunks.append(r.get("text") or f"[resource {r.get('uri')}]")
            else:
                chunks.append(f"[{kind} content omitted]")
        if res.get("structuredContent") and not chunks:
            chunks.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        return "\n".join(chunks), bool(res.get("isError"))

    # -------------------------------------------------------------- internals
    def _ensure(self) -> None:
        if not self._initialized:
            self.initialize()

    def _headers(self) -> dict[str, str]:
        s = self.settings
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL_VERSION}
        if s.api_key:
            h["Authorization"] = f"Bearer {s.api_key}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if s.mcp_servers:
            h["X-MCP-Servers"] = ",".join(s.mcp_servers)
        if s.mcp_tool_discovery:
            h["X-MCP-Tool-Discovery"] = s.mcp_tool_discovery
        return h

    def _notify(self, method: str) -> None:
        body = json.dumps({"jsonrpc": "2.0", "method": method}).encode()
        req = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            urllib.request.urlopen(req, timeout=30).close()
        except urllib.error.HTTPError:
            pass  # 202/204 or unsupported — notifications have no reply

    def _rpc(self, method: str, params: dict[str, Any], allow_reinit: bool = True) -> dict[str, Any]:
        rid = next(self._ids)
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.settings.mcp_timeout_s) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            if exc.code in {404, 400} and allow_reinit and self.session_id and "session" in detail.lower():
                self._initialized = False
                self.initialize()
                return self._rpc(method, params, allow_reinit=False)
            raise MCPError(f"MCP {method} HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise MCPError(f"cannot reach GoModel MCP gateway at {self.url}: {exc}") from exc
        message = _pick_message(raw, ctype, rid)
        if "error" in message:
            err = message["error"]
            raise MCPError(f"MCP {method} error {err.get('code')}: {err.get('message')}")
        return message.get("result") or {}


def _pick_message(raw: str, content_type: str, rid: int) -> dict[str, Any]:
    if "text/event-stream" in content_type or raw.lstrip().startswith(("event:", "data:")):
        candidates = []
        for block in raw.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if data:
                try:
                    candidates.append(json.loads(data))
                except json.JSONDecodeError:
                    continue
        for msg in candidates:
            if isinstance(msg, dict) and msg.get("id") == rid:
                return msg
        if candidates:
            return candidates[-1]
        raise MCPError("empty SSE response from MCP gateway")
    try:
        msg = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise MCPError(f"invalid JSON from MCP gateway: {raw[:300]}") from exc
    if isinstance(msg, list):
        msg = next((m for m in msg if m.get("id") == rid), msg[-1] if msg else {})
    return msg
