"""Shared fixtures: a scripted LLM, fake GoModel chat + MCP HTTP servers, a temp project."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bmad_adapter.llm_client import LLMResponse, ToolCall  # noqa: E402

BMAD_ROOT = os.environ.get("BMAD_TEST_ROOT") or str(
    Path(__file__).resolve().parents[2] / "Plugins" / "BMAD-METHOD-6.12.4" / "BMAD-METHOD-6.12.4")
if not Path(BMAD_ROOT, "src").is_dir():
    BMAD_ROOT = "/home/claude/ref/bmad"


def call(name: str, **args: Any) -> ToolCall:
    call.n = getattr(call, "n", 0) + 1
    return ToolCall(id=f"c{call.n}", name=name, arguments=args, raw_arguments=json.dumps(args))


def say(text: str) -> LLMResponse:
    return LLMResponse(content=text)


def tools(*calls: ToolCall, text: str | None = None) -> LLMResponse:
    return LLMResponse(content=text, tool_calls=list(calls))


class ScriptedLLM:
    """Policy-driven fake model: ``policy(messages, tool_names) -> LLMResponse``."""

    def __init__(self, policy: Callable[[list[dict], list[str]], LLMResponse]) -> None:
        self.policy = policy
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def chat(self, messages, tools=None, *, model=None, temperature=None, max_tokens=None):
        names = [t["function"]["name"] for t in tools or []]
        with self.lock:
            self.calls.append({"messages": messages, "tools": names, "model": model})
        return self.policy(messages, names)


def last_tool_result(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "tool":
            return m["content"]
    return ""


def is_subagent(messages: list[dict]) -> bool:
    return "BMAD Subagent" in messages[0]["content"]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    p = tmp_path / "habit-app"
    p.mkdir()
    return p


@pytest.fixture()
def settings_for(project: Path):
    from bmad_adapter.settings import AdapterSettings

    def make(**extra: Any) -> AdapterSettings:
        base = {"bmad_root": BMAD_ROOT, "project_root": str(project),
                "gomodel": {"mcp_enabled": False}, "install": {"user_name": "Ravi"}}
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                base[k] = {**base[k], **v}
            else:
                base[k] = v
        return AdapterSettings.load(None, **base)
    return make


# ----------------------------------------------------------------- fake GoModel
class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeGoModel/1"

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})  # type: ignore[attr-defined]
        if self.path == "/v1/chat/completions":
            self._json(200, self.server.chat_reply(body))  # type: ignore[attr-defined]
            return
        if self.path.startswith("/mcp"):
            if "id" not in body:                       # notification
                self.send_response(202)
                self.end_headers()
                return
            result = self.server.mcp_reply(body)  # type: ignore[attr-defined]
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            if body["method"] == "tools/call" and self.server.sse:  # type: ignore[attr-defined]
                payload = f"event: message\ndata: {json.dumps(msg)}\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Mcp-Session-Id", "sess-123")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._json(200, msg, extra={"Mcp-Session-Id": "sess-123"})
            return
        self._json(404, {"error": "nope"})

    def _json(self, code: int, obj: Any, extra: dict | None = None) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_gomodel():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests = []  # type: ignore[attr-defined]
    srv.sse = True  # type: ignore[attr-defined]

    def chat_reply(body):
        if body.get("tools"):
            return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "t1", "type": "function",
                                    "function": {"name": "duckduckgo_search", "arguments": "{\"query\": \"x\"}"}}]}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1}}

    def mcp_reply(body):
        m = body["method"]
        if m == "initialize":
            return {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "gomodel"}}
        if m == "tools/list":
            if not body.get("params", {}).get("cursor"):
                return {"tools": [{"name": "duckduckgo_search", "description": "Web search",
                                   "inputSchema": {"$schema": "x", "type": "object",
                                                   "properties": {"query": {"type": "string"}}},
                                   "annotations": {"readOnlyHint": True}}], "nextCursor": "p2"}
            return {"tools": [{"name": "crawl4ai_crawl_url", "description": "Crawl",
                               "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}}]}
        if m == "tools/call":
            q = body["params"]["arguments"]
            return {"content": [{"type": "text", "text": f"results for {json.dumps(q)}"}], "isError": False}
        return {}

    srv.chat_reply = chat_reply  # type: ignore[attr-defined]
    srv.mcp_reply = mcp_reply  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.base_url = f"http://127.0.0.1:{srv.server_address[1]}"  # type: ignore[attr-defined]
    yield srv
    srv.shutdown()
