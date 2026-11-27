"""Agent executor — activate BMAD personas and process agent instructions.

Does deterministically what each ``bmad-agent-*`` SKILL.md "On Activation" asks for:
  Step 1  resolve the ``agent`` block (customize.toml → _bmad/custom team → user)
  Step 4  load ``persistent_facts`` (literal facts and ``file:`` globs under the project)
  Step 5  resolve central config
…then hands the agent's SKILL.md to the LLM as the base program so the remaining
judgement steps (prepend/append steps, greeting, menu dispatch, staying in character)
are interpreted faithfully. The persona stays active across every skill it invokes until
dismissed, exactly like in an IDE.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import BMadAdapterError

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import BMadRuntime
    from .session_manager import Session
    from .tool_manager import ToolContext

MAX_FACT_FILE_CHARS = 20_000


class AgentExecutor:
    def __init__(self, runtime: "BMadRuntime") -> None:
        self.rt = runtime

    def activate(self, session: "Session", ctx: "ToolContext | None", agent_ref: str) -> dict[str, Any]:
        rt = self.rt
        info = rt.registry.get_agent(agent_ref)
        loaded = rt.loader.load_agent(info)
        block = (loaded.customization or rt.config.customization(info.skill.skill_dir, "agent")).get("agent", {})  # type: ignore[union-attr]
        persona = {
            "agent_id": info.agent_id,
            "name": block.get("name", info.name),
            "title": block.get("title", info.title),
            "icon": block.get("icon", info.icon),
            "role": block.get("role", ""),
            "identity": block.get("identity", ""),
            "communication_style": block.get("communication_style", ""),
            "principles": list(block.get("principles", [])),
            "facts": self._resolve_facts(block.get("persistent_facts", [])),
            "menu": list(block.get("menu", [])),
            "activation_steps_prepend": list(block.get("activation_steps_prepend", [])),
            "activation_steps_append": list(block.get("activation_steps_append", [])),
            "skill_file": str(loaded.entry_file),
        }
        session.persona = persona
        # The agent program is the base of the skill stack; workflow skills stack on top of it.
        session.skill_stack = [{"skill_id": info.agent_id, "kind": "agent", "entry_file": str(loaded.entry_file),
                                "mode": "inline"}]
        rt.skills.cache(loaded)
        rt.workflow.on_skill_entered(session, info.agent_id, str(loaded.entry_file))
        if ctx is not None:
            ctx.skill = loaded
        return persona

    def dismiss(self, session: "Session") -> None:
        session.persona = None
        session.skill_stack = [s for s in session.skill_stack if s.get("kind") != "agent"]

    def match_menu(self, session: "Session", text: str) -> dict[str, Any] | None:
        """Number, menu code, or unambiguous fuzzy description match (agent Step 8)."""
        persona = session.persona or {}
        menu = persona.get("menu") or []
        t = text.strip().lower()
        if not t or not menu:
            return None
        if t.isdigit() and 1 <= int(t) <= len(menu):
            return menu[int(t) - 1]
        for item in menu:
            if t == str(item.get("code", "")).lower():
                return item
        words = set(re.findall(r"[a-z]{3,}", t))
        scored = []
        for item in menu:
            desc_words = set(re.findall(r"[a-z]{3,}", str(item.get("description", "")).lower()))
            skill = str(item.get("skill", "")).lower()
            score = len(words & desc_words) + (3 if skill and skill.removeprefix("bmad-") in t else 0)
            scored.append((score, item))
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] >= 2 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        return None

    # ------------------------------------------------------------ internals
    def _resolve_facts(self, entries: list[Any]) -> list[str]:
        facts: list[str] = []
        root = self.rt.project_root
        for entry in entries:
            if not isinstance(entry, str):
                continue
            if entry.startswith("file:"):
                pattern = entry[5:].replace("{project-root}", str(root))
                if not Path(pattern).is_absolute():
                    pattern = str(root / pattern)
                for path in sorted(glob.glob(pattern, recursive=True))[:20]:
                    p = Path(path)
                    try:
                        self.rt.permissions.check_read(p)
                        text = p.read_text(encoding="utf-8", errors="replace")[:MAX_FACT_FILE_CHARS]
                        facts.append(f"[from {p}]\n{text}")
                    except (OSError, BMadAdapterError):
                        continue
            elif entry.startswith("skill:"):
                facts.append(f"(load on demand with invoke_skill: {entry[6:].strip()})")
            else:
                facts.append(entry)
        return facts
