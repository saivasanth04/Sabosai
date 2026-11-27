"""Session manager — save, pause and resume execution.

A session is the full execution state of one BMAD conversation: message history,
compacted memory, active persona, skill stack, workflow state, artifacts and audit. It is
written atomically to ``{project}/_bmad/runtime/sessions/<id>.json`` after every turn, so
a crash or a deliberate pause loses nothing and ``resume()`` continues exactly where the
workflow stopped (BMAD's own continuation markers — ``stepsCompleted`` frontmatter and
``.memlog.md`` — live in the project files and are re-read by the skill on resume).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import SessionError

ACTIVE, WAITING, PAUSED, COMPLETED, BLOCKED, FAILED = (
    "active", "waiting_input", "paused", "completed", "blocked", "failed")


@dataclass
class Session:
    session_id: str
    project_root: str
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    status: str = ACTIVE
    title: str = ""
    headless: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    memory: str = ""                                  # compacted earlier conversation
    persona: dict[str, Any] | None = None             # active BMAD agent
    skill_stack: list[dict[str, Any]] = field(default_factory=list)
    workflow: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0})
    metadata: dict[str, Any] = field(default_factory=dict)
    ephemeral: bool = False                           # subagent sessions are not persisted

    @property
    def active_skill(self) -> dict[str, Any] | None:
        return self.skill_stack[-1] if self.skill_stack else None

    def add_usage(self, usage: dict[str, Any]) -> None:
        self.usage["llm_calls"] = self.usage.get("llm_calls", 0) + 1
        for k in ("prompt_tokens", "completion_tokens"):
            self.usage[k] = self.usage.get(k, 0) + int(usage.get(k) or 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def brief(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "status": self.status, "title": self.title,
                "persona": (self.persona or {}).get("agent_id"),
                "skill": (self.active_skill or {}).get("skill_id"),
                "step": self.workflow.get("current_step"), "updated": self.updated,
                "messages": len(self.messages)}


class SessionManager:
    def __init__(self, session_dir: Path) -> None:
        self.dir = Path(session_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._live: dict[str, Session] = {}

    def create(self, project_root: Path, *, headless: bool = False, title: str = "",
               session_id: str | None = None, ephemeral: bool = False) -> Session:
        sid = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        s = Session(session_id=sid, project_root=str(project_root), headless=headless, title=title,
                    ephemeral=ephemeral)
        if not ephemeral:
            self._live[sid] = s
            self.save(s)
        return s

    def path(self, session_id: str) -> Path:
        if not session_id or any(c in session_id for c in "/\\.."):
            raise SessionError(f"invalid session id {session_id!r}")
        return self.dir / f"{session_id}.json"

    def save(self, session: Session) -> None:
        if session.ephemeral:
            return
        session.updated = time.time()
        data = json.dumps(session.to_dict(), ensure_ascii=False, indent=1, default=str)
        with self._lock:
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self.dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path(session.session_id))
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def load(self, session_id: str) -> Session:
        if session_id in self._live:
            return self._live[session_id]
        p = self.path(session_id)
        if not p.exists():
            raise SessionError(f"no such session: {session_id}")
        try:
            s = Session.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError) as exc:
            raise SessionError(f"corrupt session file {p}: {exc}") from exc
        self._live[session_id] = s
        return s

    def pause(self, session_id: str, note: str = "") -> Session:
        s = self.load(session_id)
        s.metadata["paused_from"] = s.status
        s.status = PAUSED
        if note:
            s.metadata["pause_note"] = note
        self.save(s)
        return s

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                s = Session.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
            if status is None or s.status == status:
                out.append(s.brief())
        return out

    def latest(self, status: str | None = None) -> Session | None:
        items = self.list(status)
        return self.load(items[0]["session_id"]) if items else None
