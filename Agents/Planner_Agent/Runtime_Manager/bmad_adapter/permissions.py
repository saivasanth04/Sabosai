"""Permission manager — controls filesystem, terminal (script) and tool access.

Policy
  * Reads:   BMAD clone, project root, extra_read_roots.
  * Writes:  project root (+ extra_write_roots). Never the BMAD clone, never
             ``{project}/_bmad/scripts`` (installed runtime), never ``.git``.
  * Scripts: only ``.py`` files under the BMAD clone or ``{project}/_bmad`` unless
             ``allow_arbitrary_scripts``.
  * Tools:   fnmatch allow / deny / ask lists over tool names (built-in and GoModel MCP).
  * Modes:   ``auto`` (policy only), ``ask`` (every write/script/MCP call goes through the
             approval callback), ``readonly`` (no writes, scripts limited to read-only resolvers).

Every decision is appended to an in-memory audit log (persisted with the session).
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import PermissionDeniedError
from .settings import PermissionSettings

READONLY_SCRIPTS = {"resolve_config.py", "resolve_customization.py", "list_customizable_skills.py"}


@dataclass
class PermissionRequest:
    category: str            # read | write | script | tool
    target: str
    detail: dict[str, Any] = field(default_factory=dict)


ApprovalCallback = Callable[[PermissionRequest], bool]


@dataclass
class AuditEntry:
    ts: float
    category: str
    target: str
    allowed: bool
    reason: str


class PermissionManager:
    def __init__(self, settings: PermissionSettings, bmad_root: Path, project_root: Path,
                 approval_callback: ApprovalCallback | None = None) -> None:
        self.settings = settings
        self.bmad_root = Path(bmad_root).resolve()
        self.project_root = Path(project_root).resolve()
        self.approval_callback = approval_callback
        self.read_roots = [self.bmad_root, self.project_root,
                           *(Path(p).expanduser().resolve() for p in settings.extra_read_roots)]
        self.write_roots = [self.project_root, *(Path(p).expanduser().resolve() for p in settings.extra_write_roots)]
        self.protected = [self.project_root / "_bmad" / "scripts", self.project_root / ".git"]
        if settings.protect_bmad_source:
            self.protected.append(self.bmad_root)
        self.audit: list[AuditEntry] = []
        self._session_grants: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ filesystem
    def resolve(self, path: str | Path, base: Path | None = None) -> Path:
        p = Path(str(path)).expanduser()
        if not p.is_absolute():
            p = (base or self.project_root) / p
        return p.resolve()

    def check_read(self, path: str | Path, base: Path | None = None) -> Path:
        p = self.resolve(path, base)
        if not any(_within(p, r) for r in self.read_roots + self.write_roots):
            self._deny("read", p, "outside readable roots")
        self._log("read", p, True, "ok")
        return p

    def check_write(self, path: str | Path, base: Path | None = None) -> Path:
        p = self.resolve(path, base)
        if self.settings.mode == "readonly":
            self._deny("write", p, "permission mode is readonly")
        if not any(_within(p, r) for r in self.write_roots):
            self._deny("write", p, f"writes are limited to {', '.join(map(str, self.write_roots))}")
        for prot in self.protected:
            if _within(p, prot):
                self._deny("write", p, f"protected location {prot}")
        self._maybe_ask(PermissionRequest("write", str(p)), always=self.settings.mode == "ask")
        self._log("write", p, True, "ok")
        return p

    # ---------------------------------------------------------------- scripts
    def check_script(self, script: Path, args: list[str]) -> None:
        script = script.resolve()
        allowed_roots = [self.bmad_root, self.project_root / "_bmad"]
        if not self.settings.allow_arbitrary_scripts and not any(_within(script, r) for r in allowed_roots):
            self._deny("script", script, "only BMAD scripts (BMAD clone or {project}/_bmad) may run")
        if self.settings.mode == "readonly" and script.name not in READONLY_SCRIPTS:
            self._deny("script", script, "readonly mode allows only config resolvers")
        self._maybe_ask(PermissionRequest("script", str(script), {"args": args}),
                        always=self.settings.mode == "ask" and script.name not in READONLY_SCRIPTS)
        self._log("script", script, True, "ok")

    # --------------------------------------------------------------- commands
    def check_command(self, line: str, cwd: Path) -> None:
        if not self.settings.allow_commands:
            self._deny("command", line, "developer commands are disabled (permissions.allow_commands = false)")
        if self.settings.mode == "readonly":
            self._deny("command", line, "readonly mode")
        self._maybe_ask(PermissionRequest("command", line, {"cwd": str(cwd)}), always=self.settings.mode == "ask")
        self._log("command", line, True, f"cwd={cwd}")

    # ------------------------------------------------------------------ tools
    def check_tool(self, name: str, arguments: dict[str, Any] | None = None, *, external: bool = False) -> None:
        s = self.settings
        if any(fnmatch.fnmatch(name, pat) for pat in s.deny_tools):
            self._deny("tool", name, "matched deny_tools")
        if not any(fnmatch.fnmatch(name, pat) for pat in s.allow_tools):
            self._deny("tool", name, "not in allow_tools")
        must_ask = any(fnmatch.fnmatch(name, pat) for pat in s.ask_tools) or (external and s.mode == "ask")
        self._maybe_ask(PermissionRequest("tool", name, {"arguments": arguments or {}}), always=must_ask)
        self._log("tool", name, True, "ok")

    # --------------------------------------------------------------- internals
    def _maybe_ask(self, req: PermissionRequest, *, always: bool) -> None:
        if not always:
            return
        key = (req.category, req.target)
        if key in self._session_grants:
            return
        if self.approval_callback is None:
            self._deny(req.category, req.target, "approval required but no approval callback is configured")
        if not self.approval_callback(req):
            self._deny(req.category, req.target, "declined by approver")
        self._session_grants.add(key)

    def _deny(self, category: str, target: Any, reason: str) -> None:
        self._log(category, target, False, reason)
        raise PermissionDeniedError(f"{category} denied for {target}: {reason}")

    def _log(self, category: str, target: Any, allowed: bool, reason: str) -> None:
        self.audit.append(AuditEntry(time.time(), category, str(target), allowed, reason))
        if len(self.audit) > 2000:
            del self.audit[:500]


def _within(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except ValueError:  # different drives on Windows
        return False
