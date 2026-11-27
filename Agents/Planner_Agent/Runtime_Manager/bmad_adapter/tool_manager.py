"""Tool manager — one registry for runtime capabilities and GoModel MCP tools.

* Built-in tools (file I/O, run_script, invoke_skill, spawn_subagent, ask_user, …) are
  registered by :mod:`bmad_adapter.builtin_tools`.
* GoModel MCP tools (``duckduckgo_search``, ``crawl4ai_crawl_url``, ``github_get_file_contents``,
  ``context7_query-docs`` …) are discovered from the gateway's ``tools/list`` and proxied.
* Every call goes through the permission manager, is time-boxed by the underlying
  executor, truncated for context safety, and errors are returned to the model as text so
  it can recover (BMAD skills are written to "skip and flag unavailable tools").
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .errors import BMadAdapterError, MCPError, PermissionDeniedError

if TYPE_CHECKING:  # pragma: no cover
    from .loader import LoadedBMadSkill
    from .mcp_client import GoModelMCPClient
    from .permissions import PermissionManager
    from .session_manager import Session

log = logging.getLogger(__name__)
_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Tool groups, used to scope subagents and modes
READ_ONLY_TOOLS = {"read_file", "read_multiple_files", "list_dir", "directory_tree", "file_info", "search_files",
                   "list_bmad_skills"}
WRITE_TOOLS = {"write_file", "append_file", "edit_file", "create_directory", "move_file", "copy_file"}
CONTROL_TOOLS = {"ask_user", "complete_workflow", "invoke_skill", "spawn_subagent", "spawn_subagents",
                 "activate_agent", "dismiss_agent"}


@dataclass
class ToolContext:
    session: "Session"
    skill: "LoadedBMadSkill | None" = None
    depth: int = 0                              # 0 = main conversation, >0 = subagent
    headless: bool = False
    read_only: bool = False
    allowed: set[str] | None = None             # None = everything registered
    yield_requested: bool = False               # set by ask_user / complete_workflow
    yield_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def request_yield(self, reason: str) -> None:
        self.yield_requested = True
        self.yield_reason = reason


ToolHandler = Callable[[dict[str, Any], ToolContext], str]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    source: str = "builtin"                     # builtin | mcp
    group: str = "general"                      # read | write | control | script | mcp
    external: bool = False

    def schema(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description[:1024],
                             "parameters": self.parameters or {"type": "object", "properties": {}}}}


class ToolManager:
    def __init__(self, permissions: "PermissionManager", mcp: "GoModelMCPClient | None" = None,
                 result_max_chars: int = 24_000) -> None:
        self.permissions = permissions
        self.mcp = mcp
        self.result_max_chars = result_max_chars
        self._tools: dict[str, Tool] = {}
        self.mcp_status: str = "not connected"
        self.mcp_upstream: dict[str, str] = {}          # registered name → upstream (gateway) tool name
        self.hidden_prefixes: list[str] = []            # e.g. ["filesystem_"]: routed via the fs backend
        self.call_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------- registry
    def register(self, tool: Tool) -> None:
        if not _NAME_OK.match(tool.name):
            raise ValueError(f"invalid tool name for OpenAI tool calling: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def connect_mcp(self) -> int:
        """Discover GoModel MCP tools. Returns the number registered (0 on failure)."""
        if self.mcp is None:
            self.mcp_status = "disabled"
            return 0
        try:
            tools = self.mcp.list_tools()
        except MCPError as exc:
            self.mcp_status = f"unavailable: {exc}"
            log.warning("GoModel MCP gateway unavailable: %s", exc)
            return 0
        count = 0
        for t in tools:
            name = t.name if _NAME_OK.match(t.name) else re.sub(r"[^A-Za-z0-9_-]", "_", t.name)[:64]
            if name in self._tools and self._tools[name].source == "builtin":
                name = f"mcp_{name}"[:64]
            upstream = t.name
            read_only_hint = bool(t.annotations.get("readOnlyHint"))

            def handler(args: dict[str, Any], ctx: ToolContext, _n=upstream) -> str:
                text, is_error = self.mcp.call_tool(_n, args)  # type: ignore[union-attr]
                return ("ERROR from MCP tool: " if is_error else "") + text

            hidden = any(upstream.startswith(pfx) for pfx in self.hidden_prefixes)
            self.mcp_upstream[name] = upstream
            self.register(Tool(name=name, description=f"[GoModel MCP] {t.description}",
                               parameters=_clean_schema(t.input_schema), handler=handler, source="mcp",
                               group="mcp-hidden" if hidden else ("mcp-read" if read_only_hint else "mcp"),
                               external=True))
            count += 1
        self.mcp_status = f"connected ({count} tools)"
        return count

    # --------------------------------------------------------------- schemas
    def schemas(self, ctx: ToolContext) -> list[dict[str, Any]]:
        return [t.schema() for t in self._visible(ctx)]

    def _visible(self, ctx: ToolContext) -> list[Tool]:
        out = []
        for t in self._tools.values():
            if ctx.allowed is not None and t.name not in ctx.allowed and not any(
                    fnmatch.fnmatch(t.name, p) for p in ctx.allowed if "*" in p):
                continue
            if t.group == "mcp-hidden":
                continue
            if ctx.read_only and (t.group in {"write"} or t.name in WRITE_TOOLS):
                continue
            if ctx.depth > 0 and t.name in {"ask_user", "activate_agent", "dismiss_agent"}:
                continue                      # subagents never talk to the human directly
            out.append(t)
        return sorted(out, key=lambda t: (t.source != "builtin", t.name))

    def mcp_tool_names(self) -> list[str]:
        """MCP tools agents can see (raw filesystem tools are routed through the fs backend instead)."""
        return sorted(n for n, t in self._tools.items() if t.source == "mcp" and t.group != "mcp-hidden")

    def upstream_names(self, prefix: str) -> set[str]:
        """Upstream tool names exposed by one GoModel server, without the ``<slug>_`` prefix."""
        return {u[len(prefix):] for u in self.mcp_upstream.values() if u.startswith(prefix)}

    # --------------------------------------------------------------- execute
    def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
        start = time.monotonic()
        tool = self._tools.get(name)
        if tool is None or tool not in self._visible(ctx):
            return f"ERROR: tool `{name}` is not available here. Available: {', '.join(t.name for t in self._visible(ctx))}"
        if "__invalid_json__" in arguments:
            return f"ERROR: tool arguments were not valid JSON: {arguments['__invalid_json__'][:300]}"
        ok = True
        try:
            self.permissions.check_tool(name, arguments, external=tool.external)
            result = tool.handler(arguments, ctx)
        except PermissionDeniedError as exc:
            ok, result = False, f"PERMISSION DENIED: {exc}"
        except BMadAdapterError as exc:
            ok, result = False, f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface every failure to the model
            log.exception("tool %s crashed", name)
            ok, result = False, f"ERROR: {type(exc).__name__}: {exc}"
        result = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        if len(result) > self.result_max_chars:
            result = result[: self.result_max_chars] + f"\n…[truncated {len(result) - self.result_max_chars} chars]"
        entry = {"tool": name, "ok": ok, "ms": int((time.monotonic() - start) * 1000), "depth": ctx.depth,
                 "session": ctx.session.session_id if ctx.session else None}
        self.call_log.append(entry)
        ctx.events.append({"type": "tool", **entry})
        return result


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI tool schemas must be objects; strip $schema noise from MCP schemas."""
    s = dict(schema or {})
    s.pop("$schema", None)
    if s.get("type") != "object":
        s = {"type": "object", "properties": s.get("properties", {})}
    s.setdefault("properties", {})
    return s
