"""Instruction engine — interpret BMAD instructions through the LLM.

The agentic loop: build context → call GoModel with tools → execute tool calls → repeat,
until the model ends its turn, a tool asks to yield (``ask_user`` / ``complete_workflow``),
or the turn budget is exhausted. Used for the main conversation *and* for subagents
(each with its own Session / ToolContext), so behaviour is identical in both.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .errors import LLMError
from .session_manager import ACTIVE, COMPLETED, FAILED, WAITING

if TYPE_CHECKING:  # pragma: no cover
    from .context_manager import ContextManager
    from .llm_client import ChatClient
    from .session_manager import Session, SessionManager
    from .tool_manager import ToolContext, ToolManager
    from .workflow_engine import WorkflowEngine

log = logging.getLogger(__name__)
SystemPromptBuilder = Callable[["ToolContext"], str]


@dataclass
class EngineResult:
    status: str                         # waiting_input | completed | blocked | active | failed
    text: str                           # last assistant text shown to the user
    turns: int = 0
    tool_calls: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class InstructionEngine:
    def __init__(self, llm: "ChatClient", tools: "ToolManager", context: "ContextManager",
                 workflow: "WorkflowEngine", sessions: "SessionManager | None", *,
                 max_turns: int = 60, max_tool_calls_per_turn: int = 16, model: str | None = None) -> None:
        self.llm = llm
        self.tools = tools
        self.context = context
        self.workflow = workflow
        self.sessions = sessions
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.model = model
        self.on_event: Callable[[dict[str, Any]], None] | None = None   # streaming hook for UIs

    def run(self, ctx: "ToolContext", build_system_prompt: SystemPromptBuilder,
            user_message: str | None = None, *, max_turns: int | None = None) -> EngineResult:
        session = ctx.session
        if user_message is not None:
            session.messages.append({"role": "user", "content": user_message})
        session.status = ACTIVE
        last_text = ""
        turns = calls = 0
        limit = max_turns or self.max_turns
        while turns < limit:
            turns += 1
            ctx.yield_requested, ctx.yield_reason = False, None
            system_prompt = build_system_prompt(ctx)
            self.context.ensure_budget(session, system_prompt)
            messages = self.context.build(session, system_prompt)
            try:
                resp = self.llm.chat(messages, self.tools.schemas(ctx) or None, model=self.model)
            except LLMError as exc:
                self.workflow.on_failure(session, str(exc))
                session.status = FAILED
                self._save(session)
                return EngineResult(FAILED, last_text, turns, calls, ctx.events, error=str(exc))
            session.add_usage(resp.usage)
            tool_calls = resp.tool_calls[: self.max_tool_calls_per_turn]
            resp.tool_calls = tool_calls
            session.messages.append(resp.as_message())
            if resp.content:
                last_text = resp.content
                self._emit(ctx, {"type": "assistant", "text": resp.content})

            if not tool_calls:
                outcome = self.workflow.on_assistant_final(session, resp.content or "", ctx.headless or ctx.depth > 0)
                if outcome == "needs_nudge" and ctx.depth == 0:
                    session.messages.append({"role": "user", "content": self.workflow.nudge(session)})
                    self._save(session)
                    continue
                status = COMPLETED if outcome in {"needs_nudge", COMPLETED} else outcome
                session.status = status
                self._save(session)
                return EngineResult(status, last_text, turns, calls, ctx.events)

            for tc in tool_calls:
                calls += 1
                self._emit(ctx, {"type": "tool_call", "name": tc.name, "arguments": tc.arguments})
                result = self.tools.execute(tc.name, tc.arguments, ctx)
                session.messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                self._emit(ctx, {"type": "tool_result", "name": tc.name, "preview": result[:300]})
            self._save(session)
            if ctx.yield_requested:
                st = self.workflow.state(session)
                status = st.status if st.status != ACTIVE else WAITING
                session.status = status
                self._save(session)
                if ctx.yield_reason == "skill_returned":      # nested skill finished → keep going
                    continue
                return EngineResult(status, last_text or (st.pending_question or {}).get("question", ""),
                                    turns, calls, ctx.events)
        session.status = ACTIVE
        self._save(session)
        log.info("turn budget (%s) exhausted; session stays active and can be continued", limit)
        return EngineResult(ACTIVE, last_text, turns, calls, ctx.events,
                            error=f"turn budget of {limit} model calls reached — call send()/resume() to continue")

    def _save(self, session: "Session") -> None:
        if self.sessions is not None:
            self.sessions.save(session)

    def _emit(self, ctx: "ToolContext", event: dict[str, Any]) -> None:
        event = {**event, "depth": ctx.depth}
        ctx.events.append(event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                log.debug("on_event hook failed", exc_info=True)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
