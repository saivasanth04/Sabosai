"""Workflow engine — continuation, decisions and execution state.

BMAD workflows are *step-file* programs written in Markdown: "read fully and follow
step-02…", "HALT and wait for input", "if the user picks C, continue", "update
stepsCompleted in frontmatter". The LLM interprets the prose; this engine keeps the
deterministic state around it:

* **Step tracking** — every ``read_file`` of a ``step-*.md`` / ``steps/*.md`` /
  ``workflow.md`` moves ``current_step`` and appends to ``steps_visited``; BMAD's rule
  "never load multiple step files simultaneously" is enforced with a warning.
* **Checkpoints** — ``ask_user`` pauses the run (``waiting_input``) with the pending
  question and options; :meth:`answer` resumes it.
* **Completion** — ``complete_workflow`` or a headless JSON status payload
  (``{"status": "complete|partial|blocked", ...}``) ends a skill; nested skills pop back
  to their caller.
* **Decisions** — memlog ``append`` calls and explicit decisions are mirrored into
  ``decisions`` so the planner can read them without parsing files.
* **Continuation** — a short state block is injected into every system prompt so the
  model knows where it is after compaction or a resume, and headless runs that stop
  without finishing get a bounded number of "continue" nudges.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .session_manager import ACTIVE, BLOCKED, COMPLETED, FAILED, WAITING, Session

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_STATUS_VALUES = {"complete", "completed", "partial", "blocked"}
MAX_HEADLESS_NUDGES = 3


@dataclass
class WorkflowState:
    status: str = ACTIVE
    current_skill: str | None = None
    current_step: str | None = None
    steps_visited: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    pending_question: dict[str, Any] | None = None
    completion: dict[str, Any] | None = None
    completed_skills: list[dict[str, Any]] = field(default_factory=list)
    nudges: int = 0
    warnings: list[str] = field(default_factory=list)


class WorkflowEngine:
    # ------------------------------------------------------------ state I/O
    @staticmethod
    def state(session: Session) -> WorkflowState:
        known = WorkflowState.__dataclass_fields__
        return WorkflowState(**{k: v for k, v in (session.workflow or {}).items() if k in known})

    @staticmethod
    def _store(session: Session, st: WorkflowState) -> None:
        session.workflow = asdict(st)

    # -------------------------------------------------------------- events
    def on_skill_entered(self, session: Session, skill_id: str, entry_file: str | None) -> None:
        st = self.state(session)
        st.current_skill = skill_id
        st.status = ACTIVE
        st.completion = None
        st.nudges = 0
        if entry_file:
            st.current_step = entry_file
            st.steps_visited.append({"skill": skill_id, "file": entry_file, "ts": time.time()})
        self._store(session, st)

    def on_file_read(self, session: Session, path: Path) -> str | None:
        """Track step navigation; returns a warning for the model if a rule is bent."""
        name = path.name
        is_step = name.startswith("step-") or path.parent.name == "steps" or name == "workflow.md"
        if not is_step:
            return None
        st = self.state(session)
        warning = None
        prev = st.current_step
        if prev and prev != str(path) and Path(prev).name.startswith("step-") and name.startswith("step-"):
            prev_num, new_num = _step_number(Path(prev).name), _step_number(name)
            if prev_num is not None and new_num is not None and new_num > prev_num + 1 and not st.pending_question:
                warning = (f"NOTE: you jumped from {Path(prev).name} to {name}. BMAD forbids skipping steps — "
                           "make sure every intermediate step was completed in order.")
                st.warnings.append(warning)
        st.current_step = str(path)
        st.steps_visited.append({"skill": st.current_skill, "file": str(path), "ts": time.time()})
        st.steps_visited = st.steps_visited[-200:]
        self._store(session, st)
        return warning

    def on_ask_user(self, session: Session, question: str, options: list[str] | None, context: str | None) -> None:
        st = self.state(session)
        st.pending_question = {"question": question, "options": options or [], "context": context,
                               "step": st.current_step, "ts": time.time()}
        st.status = WAITING
        self._store(session, st)

    def answer(self, session: Session, text: str) -> None:
        st = self.state(session)
        if st.pending_question:
            st.decisions.append({"type": "answer", "question": st.pending_question.get("question"),
                                 "answer": text, "step": st.current_step, "ts": time.time()})
        st.pending_question = None
        st.status = ACTIVE
        st.nudges = 0
        self._store(session, st)

    def on_decision(self, session: Session, kind: str, text: str) -> None:
        st = self.state(session)
        st.decisions.append({"type": kind, "text": text, "skill": st.current_skill,
                             "step": st.current_step, "ts": time.time()})
        st.decisions = st.decisions[-500:]
        self._store(session, st)

    def on_complete(self, session: Session, status: str, summary: str, artifacts: list[str],
                    result: dict[str, Any] | None) -> dict[str, Any]:
        st = self.state(session)
        norm = "complete" if status in {"complete", "completed"} else status
        st.completion = {"status": norm, "summary": summary, "artifacts": artifacts,
                         "result": result or {}, "skill": st.current_skill, "ts": time.time()}
        st.completed_skills.append(st.completion)
        st.pending_question = None
        st.status = BLOCKED if norm == "blocked" else COMPLETED
        self._store(session, st)
        return st.completion

    def resume_parent(self, session: Session, parent_skill: str | None) -> None:
        """A nested skill returned: the caller's workflow continues where it left off."""
        st = self.state(session)
        st.current_skill = parent_skill
        st.status = ACTIVE
        last = [v["file"] for v in st.steps_visited if v.get("skill") == parent_skill]
        st.current_step = last[-1] if last else None
        self._store(session, st)

    def on_failure(self, session: Session, reason: str) -> None:
        st = self.state(session)
        st.status = FAILED
        st.warnings.append(reason)
        self._store(session, st)

    def on_assistant_final(self, session: Session, text: str, headless: bool) -> str:
        """Model ended its turn without tool calls. Decide what that means."""
        st = self.state(session)
        payload = extract_status_json(text)
        if payload is not None:
            self._store(session, st)
            self.on_complete(session, str(payload.get("status")), payload.get("summary") or
                             payload.get("reason") or "", _collect_paths(payload), payload)
            return self.state(session).status
        if headless:
            return "needs_nudge" if st.nudges < MAX_HEADLESS_NUDGES else COMPLETED
        # Interactive BMAD: a final assistant message is a conversational checkpoint.
        if not st.pending_question:
            st.pending_question = {"question": _last_question(text), "options": [], "context": None,
                                   "step": st.current_step, "ts": time.time(), "implicit": True}
        st.status = WAITING
        self._store(session, st)
        return WAITING

    def nudge(self, session: Session) -> str:
        st = self.state(session)
        st.nudges += 1
        self._store(session, st)
        return ("[runtime] You are running HEADLESS — there is no human to reply. Do not wait for input. "
                "Continue the workflow from the current step using the inputs provided; record anything you "
                "had to infer as an assumption. When the skill's work is done, call `complete_workflow` "
                "with the status JSON the skill's headless instructions specify.")

    # ---------------------------------------------------------- prompt text
    def state_block(self, session: Session) -> str:
        st = self.state(session)
        if not st.current_skill and not session.persona:
            return ""
        lines = ["## Runtime workflow state (maintained by the adapter)"]
        if session.persona:
            lines.append(f"- Active persona: {session.persona.get('icon', '')} {session.persona.get('name')} "
                         f"({session.persona.get('agent_id')})")
        stack = " → ".join(s["skill_id"] for s in session.skill_stack) or "none"
        lines.append(f"- Skill stack: {stack}")
        if st.current_step:
            lines.append(f"- Current step file: `{st.current_step}` (re-read it with read_file if unsure "
                         "where you are; do not restart from step 1)")
        visited = [Path(v["file"]).name for v in st.steps_visited if v.get("skill") == st.current_skill]
        if visited:
            lines.append(f"- Steps visited this skill: {', '.join(dict.fromkeys(visited))}")
        if st.pending_question:
            lines.append(f"- Waiting on the user for: {st.pending_question.get('question')}")
        recent = [d for d in st.decisions[-8:]]
        if recent:
            lines.append("- Recent decisions/answers:")
            for d in recent:
                lines.append(f"  - ({d.get('type')}) {d.get('text') or d.get('answer')}"[:300])
        if st.completed_skills:
            done = ", ".join(f"{c.get('skill')}={c.get('status')}" for c in st.completed_skills[-6:])
            lines.append(f"- Completed skills this session: {done}")
        return "\n".join(lines)


# --------------------------------------------------------------------- helpers
def extract_status_json(text: str | None) -> dict[str, Any] | None:
    """Find a BMAD headless status payload (```json {...} ``` or a trailing bare object)."""
    if not text:
        return None
    candidates = [m.group(1) for m in _JSON_BLOCK.finditer(text)]
    stripped = text.strip()
    if stripped.endswith("}"):
        start = stripped.rfind("\n{")
        candidates.append(stripped[start + 1:] if start != -1 else stripped)
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and str(data.get("status", "")).lower() in _STATUS_VALUES:
            data["status"] = str(data["status"]).lower()
            return data
    return None


def _collect_paths(payload: dict[str, Any]) -> list[str]:
    paths = []
    for k, v in payload.items():
        if isinstance(v, str) and (v.endswith((".md", ".html", ".yaml", ".yml", ".json")) or "/" in v and "." in Path(v).name):
            if k not in {"status", "intent", "reason", "summary", "changes_summary"}:
                paths.append(v)
    return paths


def _step_number(name: str) -> int | None:
    m = re.match(r"step-(\d+)", name)
    return int(m.group(1)) if m else None


def _last_question(text: str) -> str:
    text = (text or "").strip()
    qs = [ln.strip() for ln in text.splitlines() if ln.strip().endswith("?")]
    return qs[-1] if qs else text[-300:]
