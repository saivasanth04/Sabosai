"""Context manager — conversation history and LLM context budget.

Long BMAD runs (PRD coaching, architecture) blow past any context window. Strategy, in
order of cost:

1. **Clip stale tool output** — large ``read_file`` / MCP results older than the recent
   window are replaced by a one-line stub (the file is still on disk; the model can re-read).
2. **Summarise** — older turns are folded into ``session.memory`` by the LLM with a
   BMAD-aware prompt (decisions, user answers, files written, current step, open items).
3. **Hard trim** — if no LLM is available or summarisation fails, drop the oldest turns.

Tool-call/tool-result pairs are never split, so the message list always stays valid for
the OpenAI tool-calling protocol. The system prompt (persona, skill instructions,
workflow-state block) is rebuilt every turn and never compacted.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .settings import ExecutionSettings

if TYPE_CHECKING:  # pragma: no cover
    from .llm_client import ChatClient
    from .session_manager import Session

log = logging.getLogger(__name__)

SUMMARY_PROMPT = """You maintain the working memory of a BMAD Method workflow run.
Condense the conversation excerpt below into dense notes another model can resume from.
Keep, verbatim where short: every user decision and answer, chosen working mode/path,
facts the user supplied, file paths created or edited, the current step file and what
remains in it, open questions, [ASSUMPTION]s, and persona in effect. Drop pleasantries,
tool chatter and file contents that are already saved on disk (just name the path).
Return Markdown bullet points only."""


def estimate_tokens(obj: Any) -> int:
    if isinstance(obj, str):
        return max(1, len(obj) // 4)
    return max(1, len(json.dumps(obj, ensure_ascii=False, default=str)) // 4)


class ContextManager:
    def __init__(self, settings: ExecutionSettings, llm: "ChatClient | None" = None,
                 summary_model: str | None = None) -> None:
        self.settings = settings
        self.llm = llm
        self.summary_model = summary_model

    # ---------------------------------------------------------------- build
    def build(self, session: "Session", system_prompt: str) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if session.memory:
            msgs.append({"role": "system", "content": "## Earlier in this session (compacted memory)\n" + session.memory})
        msgs.extend(session.messages)
        return msgs

    def ensure_budget(self, session: "Session", system_prompt: str) -> bool:
        """Compact ``session`` in place if needed. Returns True when anything changed."""
        budget = self.settings.context_token_budget
        if estimate_tokens(self.build(session, system_prompt)) <= budget:
            return False
        changed = self._clip_tool_results(session)
        if estimate_tokens(self.build(session, system_prompt)) <= budget:
            return changed
        cut = self._safe_cut_index(session.messages, self.settings.compaction_keep_recent)
        if cut <= 0:
            return changed
        old, keep = session.messages[:cut], session.messages[cut:]
        summary = self._summarise(old)
        if summary:
            session.memory = (session.memory + "\n" + summary).strip()
            if estimate_tokens(session.memory) > budget // 4:
                session.memory = self._summarise([{"role": "user", "content": session.memory}]) or session.memory[-(budget * 2):]
        else:
            session.memory = (session.memory + "\n- (older turns dropped without summary)").strip()
        session.messages = keep
        return True

    # ------------------------------------------------------------- strategy
    def _clip_tool_results(self, session: "Session") -> bool:
        keep_recent = self.settings.compaction_keep_recent
        changed = False
        for msg in session.messages[:-keep_recent] if len(session.messages) > keep_recent else []:
            if msg.get("role") == "tool" and len(msg.get("content") or "") > 1200:
                head = msg["content"][:400]
                msg["content"] = head + "\n…[older tool output clipped by context manager; re-run the tool if needed]"
                changed = True
        return changed

    @staticmethod
    def _safe_cut_index(messages: list[dict[str, Any]], keep_recent: int) -> int:
        """Largest index ≤ len-keep_recent that starts a fresh user/assistant turn (no orphan tool msgs)."""
        start = max(0, len(messages) - keep_recent)
        for cut in range(start, 0, -1):           # prefer a user-turn boundary
            if messages[cut].get("role") == "user":
                return cut
        for cut in range(start, 0, -1):           # else any non-tool message keeps pairs intact
            if messages[cut].get("role") != "tool":
                return cut
        return 0

    def _summarise(self, messages: list[dict[str, Any]]) -> str | None:
        if self.llm is None or not messages:
            return None
        transcript = []
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "assistant" and m.get("tool_calls"):
                calls = ", ".join(f"{c['function']['name']}({c['function']['arguments'][:200]})" for c in m["tool_calls"])
                content = (content + f"\n[tool calls: {calls}]").strip()
            if role == "tool":
                content = content[:1500]
            transcript.append(f"### {role}\n{content}")
        text = "\n\n".join(transcript)[-120_000:]
        try:
            resp = self.llm.chat([{"role": "system", "content": SUMMARY_PROMPT},
                                  {"role": "user", "content": text}], None, model=self.summary_model,
                                 temperature=0.1, max_tokens=2000)
            return (resp.content or "").strip() or None
        except Exception as exc:  # noqa: BLE001
            log.warning("context summarisation failed: %s", exc)
            return None

