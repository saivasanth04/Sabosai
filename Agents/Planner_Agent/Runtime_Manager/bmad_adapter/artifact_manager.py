"""Artifact manager — track generated documents and workflow outputs.

Records every file written through the adapter's tools, and rescans BMAD's output folders
(planning_artifacts, implementation_artifacts, project_knowledge, output_folder) so files
written by BMAD's own scripts (``memlog.py``, ``sprint_plan.py``…) are captured too.
Each artifact is classified (prd, architecture, epics, memlog, review…), its frontmatter
``status`` is read (``draft`` / ``final`` / ``ready-for-dev``…), and the index is persisted
to ``{project}/_bmad/runtime/artifacts.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import _yaml

_RULES: list[tuple[str, str]] = [
    (r"(^|/)\.memlog\.md$", "memlog"),
    (r"(^|/)prd\.md$|(^|/)prd[-_].*\.md$", "prd"),
    (r"addendum\.md$", "addendum"),
    (r"validation-report\.(md|html)$", "validation-report"),
    (r"(^|/)review-[^/]+\.md$", "review"),
    (r"(^|/)reconcile-[^/]+\.md$", "reconciliation"),
    (r"architecture|spine", "architecture"),
    (r"epics?([-_.]|$)", "epics"),
    (r"sprint-status\.ya?ml$", "sprint-status"),
    (r"(^|/)stories?(/|[-_])|story-", "story"),
    (r"brief", "product-brief"),
    (r"prfaq", "prfaq"),
    (r"(^|/)ux|design\.md$|experience", "ux"),
    (r"spec", "spec"),
    (r"retro", "retrospective"),
    (r"research|recon", "research"),
    (r"brainstorm", "brainstorming"),
    (r"project-context|agents\.md$", "project-context"),
]


@dataclass
class Artifact:
    path: str
    kind: str
    status: str | None
    title: str | None
    sha256: str
    size: int
    created: float
    updated: float
    session_ids: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


class ArtifactManager:
    def __init__(self, project_root: Path, watch_dirs: Iterable[Path] = ()) -> None:
        self.project_root = Path(project_root).resolve()
        self.index_path = self.project_root / "_bmad" / "runtime" / "artifacts.json"
        self.watch_dirs = [Path(p) for p in watch_dirs]
        self._lock = threading.Lock()
        self._items: dict[str, Artifact] = {}
        self._load()

    def set_watch_dirs(self, dirs: Iterable[Path]) -> None:
        self.watch_dirs = [Path(p) for p in dirs]

    # ---------------------------------------------------------------- record
    def record(self, path: Path | str, *, action: str, session_id: str | None = None,
               skill_id: str | None = None) -> Artifact | None:
        p = Path(path).resolve()
        if not p.is_file() or not p.is_relative_to(self.project_root):
            return None
        rel_parts = p.relative_to(self.project_root).parts
        if rel_parts and rel_parts[0] == "_bmad":     # runtime/config internals are not artifacts
            return None
        rel = p.relative_to(self.project_root).as_posix()
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        status, title = _frontmatter_info(p, data)
        now = time.time()
        with self._lock:
            art = self._items.get(rel)
            if art is None:
                art = Artifact(rel, classify(rel), status, title, sha, len(data), now, now)
                self._items[rel] = art
            elif art.sha256 == sha and action == "scan":
                return art
            art.sha256, art.size, art.updated, art.status, art.title = sha, len(data), now, status, title or art.title
            if session_id and session_id not in art.session_ids:
                art.session_ids.append(session_id)
            if skill_id and skill_id not in art.skills:
                art.skills.append(skill_id)
            art.history.append({"ts": now, "action": action, "session": session_id, "skill": skill_id})
            art.history = art.history[-30:]
            self._save()
            return art

    def scan(self, session_id: str | None = None, skill_id: str | None = None, since: float | None = None) -> list[Artifact]:
        """Pick up files created outside the write tools (e.g. by BMAD scripts)."""
        changed = []
        for d in self.watch_dirs:
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in {".md", ".yaml", ".yml", ".json", ".html", ".csv", ".txt"}:
                    continue
                if since and p.stat().st_mtime < since:
                    continue
                rel = p.resolve().relative_to(self.project_root).as_posix() if p.resolve().is_relative_to(self.project_root) else None
                if rel is None:
                    continue
                before = self._items.get(rel)
                old_sha = before.sha256 if before else None
                art = self.record(p, action="scan", session_id=session_id, skill_id=skill_id)
                if art and art.sha256 != old_sha:
                    changed.append(art)
        return changed

    # ----------------------------------------------------------------- query
    def list(self, kind: str | None = None, session_id: str | None = None) -> list[Artifact]:
        items = list(self._items.values())
        if kind:
            items = [a for a in items if a.kind == kind]
        if session_id:
            items = [a for a in items if session_id in a.session_ids]
        return sorted(items, key=lambda a: a.updated, reverse=True)

    def latest(self, kind: str) -> Artifact | None:
        items = self.list(kind)
        return items[0] if items else None

    def as_dicts(self, **filters: Any) -> list[dict[str, Any]]:
        return [{k: v for k, v in asdict(a).items() if k != "history"} for a in self.list(**filters)]

    # ----------------------------------------------------------- persistence
    def _load(self) -> None:
        if self.index_path.exists():
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8"))
                self._items = {k: Artifact(**v) for k, v in raw.items()}
            except Exception:  # noqa: BLE001
                self._items = {}

    def _save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self._items.items()}, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.index_path)


def classify(rel_path: str) -> str:
    low = rel_path.lower()
    for pattern, kind in _RULES:
        if re.search(pattern, low):
            return kind
    return "document" if low.endswith((".md", ".html")) else "file"


def _frontmatter_info(path: Path, data: bytes) -> tuple[str | None, str | None]:
    if path.suffix.lower() not in {".md", ".markdown"}:
        return None, None
    text = data[:20_000].decode("utf-8", errors="replace")
    fm, body = _yaml.split_frontmatter(text)
    status = fm.get("status")
    title = fm.get("title")
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.M)
        title = m.group(1).strip() if m else None
    return (str(status) if status is not None else None), (str(title) if title else None)
