"""Skill executor — execute skills requested by agents, users, or other skills.

Two execution modes:

* **inline** (default) — the skill is pushed onto the session's skill stack; its
  instructions become the active program in the system prompt, the persona carries
  through, and the conversation continues. When it calls ``complete_workflow`` it is
  popped and control returns to the calling skill (or to the persona's menu).
* **isolated** — the skill runs in a subagent with a fresh context and returns a compact
  result (what BMAD wants for reviewer gates: ``skill:bmad-review lenses=…``).

Also handles BMAD's ``skill:<id> <args>`` directive syntax, v6 shims (tail-call into their
target with forwarded activation), and caches loaded skills per runtime.
"""

from __future__ import annotations

import shlex
import threading
from typing import TYPE_CHECKING, Any

from .errors import BMadAdapterError

if TYPE_CHECKING:  # pragma: no cover
    from .loader import LoadedBMadSkill
    from .runtime import BMadRuntime
    from .session_manager import Session
    from .tool_manager import ToolContext

MAX_STACK_DEPTH = 8
# Workflows that are whole BMAD phases. Inside an orchestrated step only the step's own one may run;
# the orchestrator hands the next phase to its owning agent in a fresh session.
MAJOR_SKILLS = {"bmad-product-brief", "bmad-prfaq", "bmad-prd", "bmad-ux", "bmad-architecture",
                "bmad-create-epics-and-stories", "bmad-sprint-planning", "bmad-spec", "bmad-build",
                "bmad-build-auto", "bmad-retrospective", "bmad-correct-course", "bmad-project-context"}
# Sub-skills that BMAD dispatches as reviewers/extractors: run them in a subagent when nested.
ISOLATE_WHEN_NESTED = {"bmad-review"}


def parse_skill_directive(directive: str) -> tuple[str, dict[str, str], str]:
    """``"skill:bmad-review lenses=structure,prose extra words"`` → (id, {lenses: …}, "extra words")."""
    text = directive.strip().removeprefix("skill:").strip()
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        raise BMadAdapterError("empty skill directive")
    skill_id, kv, free = parts[0], {}, []
    for p in parts[1:]:
        if "=" in p and not p.startswith("="):
            k, v = p.split("=", 1)
            kv[k] = v
        else:
            free.append(p)
    return skill_id, kv, " ".join(free)


class SkillExecutor:
    def __init__(self, runtime: "BMadRuntime") -> None:
        self.rt = runtime
        self._cache: dict[str, "LoadedBMadSkill"] = {}
        self._lock = threading.Lock()

    # --------------------------------------------------------------- cache
    def cache(self, loaded: "LoadedBMadSkill") -> None:
        with self._lock:
            self._cache[loaded.skill_id] = loaded

    def loaded(self, skill_id: str) -> "LoadedBMadSkill":
        with self._lock:
            hit = self._cache.get(skill_id)
        if hit is None:
            hit = self.rt.loader.load_skill(skill_id)
            self.cache(hit)
        return hit

    def active(self, session: "Session") -> "LoadedBMadSkill | None":
        top = session.active_skill
        return self.loaded(top["skill_id"]) if top else None

    # -------------------------------------------------------------- inline
    def enter(self, session: "Session", ctx: "ToolContext | None", skill_ref: str, *,
              user_input: str | None = None, forwarded: dict[str, Any] | None = None) -> str:
        skill_id, args, free = parse_skill_directive(skill_ref)
        info = self.rt.registry.get_skill(skill_id)
        if len(session.skill_stack) >= MAX_STACK_DEPTH:
            raise BMadAdapterError(f"skill stack too deep ({MAX_STACK_DEPTH}); finish the current skill first")
        step_skill = session.metadata.get("plan_step_skill")
        if step_skill and info.skill_id in MAJOR_SKILLS and info.skill_id != step_skill:
            nxt = session.metadata.get("plan_next") or "the next planned step"
            raise BMadAdapterError(
                f"`{info.skill_id}` is a separate BMAD phase. This session runs only `{step_skill}` (orchestrated "
                f"plan). Finish `{step_skill}` now with complete_workflow — the orchestrator then hands off to "
                f"{nxt} in a fresh session with your artifacts.")
        loaded = self.rt.loader.load_skill(info)
        self.cache(loaded)
        top = session.active_skill
        if top and top.get("skill_id") == info.skill_id and top.get("kind") != "agent":
            # Re-invoking the running skill (e.g. bmad-build for the next story): close the previous run
            # instead of stacking a second copy on top of it.
            session.skill_stack.pop()
            self.rt.workflow.on_complete(session, "complete", f"{info.skill_id} superseded by a new invocation",
                                         [], {"implicit": True})
            top = session.active_skill
        tail_call = bool(top and top.get("kind") == "shim" and
                         self.rt.registry.get_skill(top["skill_id"]).forwards_to == info.skill_id)
        if tail_call:
            session.skill_stack.pop()
        entry = {"skill_id": info.skill_id, "kind": info.kind, "entry_file": str(loaded.entry_file),
                 "mode": "inline", "args": args, "forwarded": forwarded or {}, "rendered": loaded.rendered}
        session.skill_stack.append(entry)
        self.rt.workflow.on_skill_entered(session, info.skill_id, str(loaded.entry_file))
        if ctx is not None:
            ctx.skill = loaded
        parts = [f"Skill `{info.skill_id}` is now ACTIVE (inline). Its full instructions are in your system "
                 f"prompt under 'BMAD Skill: {info.skill_id}'. Start at its activation section and follow it exactly."]
        if loaded.rendered:
            parts.append(f"This is a render-based workflow; the rendered entry is `{loaded.entry_file}`.")
        if tail_call:
            parts.append("(The deprecated shim was replaced by its target; honor the forwarded activation.)")
        if forwarded:
            parts.append("Forwarded activation (honor verbatim, skip re-deriving these): "
                         + _kv(forwarded))
        if args or free:
            parts.append(f"Directive arguments: {_kv(args)} {free}".strip())
        if user_input:
            parts.append(f"Original user input: {user_input}")
        if loaded.warnings:
            parts.append("Loader warnings: " + "; ".join(loaded.warnings))
        return "\n".join(parts)

    def complete(self, session: "Session", ctx: "ToolContext", status: str, summary: str,
                 artifacts: list[str], result: dict[str, Any] | None) -> str:
        """Pop the finished skill; tell the model where control returns."""
        top = session.active_skill
        if top is None or top.get("kind") == "agent":
            completion = self.rt.workflow.on_complete(session, status, summary, artifacts, result)
            ctx.request_yield("completed")
            return f"Recorded completion ({completion['status']})."
        finished = session.skill_stack.pop()
        completion = self.rt.workflow.on_complete(session, status, summary, artifacts, result)
        self.rt.artifacts.scan(session.session_id, finished["skill_id"])
        parent = session.active_skill
        if parent and parent.get("kind") != "agent":
            self.rt.workflow.resume_parent(session, parent["skill_id"])
            ctx.skill = self.loaded(parent["skill_id"])
            return (f"Sub-skill `{finished['skill_id']}` finished ({completion['status']}). Control returns to "
                    f"`{parent['skill_id']}`, which is NOT finished: continue its workflow from where it invoked the "
                    f"sub-skill, and call complete_workflow for `{parent['skill_id']}` itself when its own final "
                    f"step is done. Result: {summary[:800]}")
        ctx.skill = self.loaded(parent["skill_id"]) if parent else None
        if session.persona and not session.headless and ctx.depth == 0:
            self.rt.workflow.resume_parent(session, parent["skill_id"] if parent else None)
            return (f"Skill `{finished['skill_id']}` finished ({completion['status']}). You are back in the "
                    f"{session.persona.get('name')} persona: summarise the outcome and artifacts for the user, "
                    "suggest the natural next step (bmad-help knows the sequence) and wait for their choice.")
        ctx.request_yield("completed")
        return f"Skill `{finished['skill_id']}` finished ({completion['status']})."

    # ------------------------------------------------------------ isolated
    def run_isolated(self, ctx: "ToolContext", skill_ref: str, task: str, *, persona: str | None = None,
                     output_file: str | None = None, profile: str = "write") -> dict[str, Any]:
        skill_id, args, free = parse_skill_directive(skill_ref)
        loaded = self.loaded(self.rt.registry.get_skill(skill_id).skill_id)
        brief = task
        if args or free:
            brief += f"\n\nDirective arguments: {_kv(args)} {free}".rstrip()
        return self.rt.subagents.spawn(brief, ctx, skill=loaded, persona=persona, profile=profile,
                                       output_file=output_file)


def _kv(d: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in d.items()) if d else ""
