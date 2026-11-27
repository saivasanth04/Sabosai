"""Subagent manager — spawn isolated research, analysis and review agents.

BMAD leans on subagents everywhere: research digests during PRD discovery, "extract,
don't ingest" source reading, parallel reviewer gates, party-mode personas, input
reconciliation. Each subagent gets:

* a fresh, ephemeral session (no parent history) and its own tool context,
* a tool profile — ``readonly`` (files + config scripts), ``research`` (+ GoModel MCP),
  ``write`` (+ file writes, for reviewers that must write ``review-*.md``),
* optional persona and/or a BMAD skill as its program,
* a depth limit (subagents may spawn one more level) and a turn budget.

Parallel spawns run on a thread pool sized by ``execution.subagent_parallelism``.
Transcripts are kept under ``{project}/_bmad/runtime/subagents/`` for audit.
"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .instruction_engine import InstructionEngine
from .prompts import SUBAGENT_PREAMBLE, activation_block, persona_block, tools_hint
from .session_manager import Session
from .tool_manager import READ_ONLY_TOOLS, WRITE_TOOLS, ToolContext

if TYPE_CHECKING:  # pragma: no cover
    from .loader import LoadedBMadSkill
    from .runtime import BMadRuntime

MAX_DEPTH = 2
PROFILES = {
    "readonly": READ_ONLY_TOOLS | {"run_script", "complete_workflow"},
    "research": READ_ONLY_TOOLS | {"run_script", "@mcp", "complete_workflow"},
    "write": READ_ONLY_TOOLS | WRITE_TOOLS | {"run_script", "run_command", "process_status", "stop_process",
                                              "@mcp", "complete_workflow"},
    "verify": READ_ONLY_TOOLS | {"run_script", "run_command", "process_status", "stop_process", "complete_workflow"},
}


class SubagentManager:
    def __init__(self, runtime: "BMadRuntime") -> None:
        self.rt = runtime
        self.transcript_dir = runtime.project_root / "_bmad" / "runtime" / "subagents"

    def spawn(self, brief: str, parent: ToolContext, *, persona: str | None = None,
              skill: "LoadedBMadSkill | str | None" = None, profile: str = "research",
              output_file: str | None = None, max_turns: int | None = None,
              label: str | None = None) -> dict[str, Any]:
        rt = self.rt
        if parent.depth >= MAX_DEPTH:
            return {"status": "refused", "result": f"subagent depth limit ({MAX_DEPTH}) reached — do this work inline"}
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        session = Session(session_id=sub_id, project_root=str(rt.project_root), headless=True,
                          ephemeral=True, title=label or brief[:60])
        loaded = rt.skills.loaded(skill.removeprefix("skill:").split()[0]) if isinstance(skill, str) else skill
        if loaded is not None:
            session.skill_stack = [{"skill_id": loaded.skill_id, "kind": loaded.info.kind,
                                    "entry_file": str(loaded.entry_file), "mode": "isolated"}]
        persona_data = None
        if persona:
            tmp = Session(session_id=sub_id + "-p", project_root=str(rt.project_root), ephemeral=True)
            persona_data = rt.agents.activate(tmp, None, persona)
            session.persona = persona_data
        allowed = set(PROFILES.get(profile, PROFILES["research"]))
        if "@mcp" in allowed:
            allowed.discard("@mcp")
            allowed |= set(rt.tools.mcp_tool_names())
        if parent.depth + 1 < MAX_DEPTH:
            allowed |= {"spawn_subagent", "spawn_subagents"}
        if loaded is not None:
            allowed |= {"invoke_skill"} if profile == "write" else set()
        ctx = ToolContext(session=session, skill=loaded, depth=parent.depth + 1, headless=True,
                          read_only=profile != "write", allowed=allowed)

        def system_prompt(c: ToolContext) -> str:
            parts = [SUBAGENT_PREAMBLE, tools_hint(rt.tools.mcp_tool_names(), rt.tools.mcp_status),
                     activation_block(rt.config.variables(), loaded.customization if loaded else None,
                                      loaded.skill_id if loaded else None)]
            if persona_data:
                parts.append(persona_block(persona_data))
            if loaded is not None:
                parts.append("## Your program (BMAD skill — run it headless)\n" + loaded.to_prompt())
            return "\n\n".join(parts)

        engine = InstructionEngine(rt.llm, rt.tools, rt.context, rt.workflow, None,
                                   max_turns=max_turns or rt.settings.execution.subagent_max_turns,
                                   max_tool_calls_per_turn=rt.settings.execution.max_tool_calls_per_turn,
                                   model=rt.settings.gomodel.subagent_model or rt.settings.gomodel.model)
        engine.on_event = rt.engine.on_event
        task = brief
        if output_file:
            task += (f"\n\nWrite your full output to `{output_file}` with write_file, then reply ONLY with a "
                     "compact summary (verdict, top findings, file path).")
        started = time.time()
        result = engine.run(ctx, system_prompt, task)
        st = rt.workflow.state(session).completion or {}
        record = {"id": sub_id, "status": result.status, "persona": persona,
                  "skill": loaded.skill_id if loaded else None, "profile": profile,
                  "result": (st.get("summary") or result.text or "").strip(),
                  "result_json": st.get("result") or None, "output_file": output_file,
                  "tool_calls": result.tool_calls, "turns": result.turns,
                  "seconds": round(time.time() - started, 1), "error": result.error}
        self._save_transcript(session, record)
        if output_file:
            rt.artifacts.record(rt.permissions.resolve(output_file), action="subagent",
                                session_id=parent.session.session_id,
                                skill_id=loaded.skill_id if loaded else None)
        parent.events.append({"type": "subagent", "id": sub_id, "status": result.status, "depth": parent.depth + 1})
        return record

    def spawn_many(self, tasks: list[dict[str, Any]], parent: ToolContext) -> list[dict[str, Any]]:
        workers = max(1, min(self.rt.settings.execution.subagent_parallelism, len(tasks)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bmad-sub") as pool:
            futures = [pool.submit(self.spawn, t.get("brief") or t.get("task", ""), parent,
                                   persona=t.get("persona"), skill=t.get("skill"),
                                   profile=t.get("profile", "research"), output_file=t.get("output_file"),
                                   label=t.get("label")) for t in tasks]
            out = []
            for f, t in zip(futures, tasks):
                try:
                    out.append(f.result())
                except Exception as exc:  # noqa: BLE001
                    out.append({"status": "failed", "label": t.get("label"), "error": str(exc)})
            return out

    def _save_transcript(self, session: Session, record: dict[str, Any]) -> None:
        try:
            self.transcript_dir.mkdir(parents=True, exist_ok=True)
            path = Path(self.transcript_dir) / f"{record['id']}.json"
            path.write_text(json.dumps({"record": record, "messages": session.messages}, ensure_ascii=False,
                                       indent=1, default=str), encoding="utf-8")
        except OSError:
            pass
