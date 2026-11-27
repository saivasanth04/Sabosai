"""BMadRuntime — the façade your Planner Agent talks to.

Wires the components:

    Registry ─ Loader ─ ConfigResolver ─ ScriptExecutor ─ PermissionManager
        │                                        │
    AgentExecutor ─ SkillExecutor ─ SubagentManager ─ ToolManager (built-ins + GoModel MCP)
        │                 │
    InstructionEngine (GoModel LLM) ─ WorkflowEngine ─ ContextManager
        │
    SessionManager ─ ArtifactManager

Typical use from the planner::

    rt = BMadRuntime.for_project(r"C:\\Projects\\NTG\\Workspace\\my-app")
    r = rt.run_skill("bmad-prd", {"intent": "create", "brief": "..."}, headless=True)
    r.status, r.completion["result"], r.artifacts

    # or interactive with a persona
    r = rt.start(agent="pm", message="Let's write the PRD for a habit tracker")
    r = rt.send(r.session_id, "Fast path please")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent_executor import AgentExecutor
from .artifact_manager import ArtifactManager
from .builtin_tools import register_builtin_tools
from .command_executor import CommandExecutor
from .config_resolver import ConfigResolver
from .context_manager import ContextManager
from .errors import BMadAdapterError
from .instruction_engine import InstructionEngine
from .llm_client import ChatClient, GoModelChatClient
from .loader import BMadLoader
from .mcp_client import GoModelMCPClient
from .orchestrator import HandleResult, Orchestrator
from .permissions import ApprovalCallback, PermissionManager
from .prompts import RUNTIME_PREAMBLE, activation_block, mode_block, persona_block, tools_hint
from .registry import BMadRegistry
from .script_executor import ScriptExecutor
from .session_manager import ACTIVE, PAUSED, WAITING, Session, SessionManager
from .settings import AdapterSettings
from .skill_executor import SkillExecutor
from .subagent_manager import SubagentManager
from .tool_manager import ToolContext, ToolManager
from .workflow_engine import WorkflowEngine

log = logging.getLogger(__name__)
AnswerProvider = Callable[[dict[str, Any]], str]


@dataclass
class TurnResult:
    session_id: str
    status: str                                   # waiting_input | completed | blocked | active | failed | paused
    message: str                                  # what the BMAD agent said last
    pending_question: dict[str, Any] | None = None
    completion: dict[str, Any] | None = None      # {status, summary, artifacts, result(JSON)}
    artifacts: list[dict[str, Any]] = field(default_factory=list)   # created/changed this turn
    persona: str | None = None
    skill: str | None = None
    step: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def to_dict(self, include_events: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not include_events:
            d.pop("events", None)
        return d


class BMadRuntime:
    def __init__(self, settings: AdapterSettings | None = None, *, llm: ChatClient | None = None,
                 approval_callback: ApprovalCallback | None = None, answer_provider: AnswerProvider | None = None,
                 mcp_client: GoModelMCPClient | None = None, connect_mcp: bool = True, install: bool = True) -> None:
        self.settings = settings or AdapterSettings.load()
        s = self.settings
        self.bmad_root = s.resolved_bmad_root()
        self.project_root = s.resolved_project_root()
        self.answer_provider = answer_provider

        # foundation
        self.permissions = PermissionManager(s.permissions, self.bmad_root, self.project_root, approval_callback)
        self.scripts = ScriptExecutor(self.project_root, self.permissions, timeout_s=s.permissions.script_timeout_s)
        self.commands = CommandExecutor(self.project_root, self.permissions,
                                        allowed=s.permissions.allowed_commands or None,
                                        timeout_s=s.permissions.command_timeout_s)
        self.registry = BMadRegistry(self.bmad_root, self.project_root, s.extra_skill_roots)
        self.config = ConfigResolver(self.bmad_root, self.project_root, self.registry, self.scripts, s.install)
        if install:
            self.config.ensure_installed()
        self.loader = BMadLoader(self.registry, self.project_root, self.config, self.scripts, s.loader)

        # tools + model
        mcp = mcp_client if mcp_client is not None else (GoModelMCPClient(s.gomodel) if s.gomodel.mcp_enabled else None)
        self.tools = ToolManager(self.permissions, mcp, s.execution.tool_result_max_chars)
        self.llm: ChatClient = llm or GoModelChatClient(s.gomodel)

        # execution
        self.workflow = WorkflowEngine()
        self.context = ContextManager(s.execution, self.llm, s.gomodel.subagent_model)
        self.sessions = SessionManager(s.resolved_session_dir(self.project_root))
        self.artifacts = ArtifactManager(self.project_root)
        self.agents = AgentExecutor(self)
        self.skills = SkillExecutor(self)
        self.subagents = SubagentManager(self)
        self.engine = InstructionEngine(self.llm, self.tools, self.context, self.workflow, self.sessions,
                                        max_turns=s.execution.max_turns_per_message,
                                        max_tool_calls_per_turn=s.execution.max_tool_calls_per_turn,
                                        model=s.gomodel.model)
        register_builtin_tools(self)
        if install:
            v = self.config.variables()
            self.artifacts.set_watch_dirs(Path(v[k]) for k in ("output_folder", "planning_artifacts",
                                                               "implementation_artifacts", "project_knowledge") if k in v)
        if connect_mcp and mcp is not None:
            self.tools.connect_mcp()
        self._orchestrator: Orchestrator | None = None

    @property
    def orchestrator(self) -> Orchestrator:
        if self._orchestrator is None:
            self._orchestrator = Orchestrator(self)
        return self._orchestrator

    def handle(self, request: str, *, auto: bool = False, through: str = "plan") -> HandleResult:
        """Plain request in → BMAD routes it to the right agents/skills and runs them in order.

        auto=False: starts the first step interactively; pass each later turn to ``continue_plan``.
        auto=True:  runs every step headless and returns when the chain finishes or blocks.
        through="ship" also includes implementation (bmad-build) for new products.
        """
        return self.orchestrator.start(request, auto=auto, through=through)

    def continue_plan(self, turn: "TurnResult") -> HandleResult | None:
        """After ``send()`` in a routed session: advance to the next step when this one finished."""
        plan = self.orchestrator.plan_for_session(turn.session_id)
        return self.orchestrator.advance(plan, turn) if plan else None

    @classmethod
    def for_project(cls, project_root: str | Path, **kwargs: Any) -> "BMadRuntime":
        config_path = kwargs.pop("config_path", None)
        settings = AdapterSettings.load(config_path, project_root=str(project_root))
        return cls(settings, **kwargs)

    # ================================================================ prompts
    def build_system_prompt(self, ctx: ToolContext) -> str:
        session = ctx.session
        skill = self.skills.active(session)
        ctx.skill = skill
        parts = [RUNTIME_PREAMBLE,
                 mode_block(ctx.headless, self.answer_provider is not None),
                 tools_hint(self.tools.mcp_tool_names(), self.tools.mcp_status),
                 activation_block(self.config.variables(), skill.customization if skill else None,
                                  skill.skill_id if skill else None)]
        if session.persona:
            parts.append(persona_block(session.persona))
        if skill is not None:
            lower = [e["skill_id"] for e in session.skill_stack[:-1]]
            if lower:
                parts.append("## Calling skills (suspended until the active one completes)\n"
                             + " → ".join(lower))
            parts.append(skill.to_prompt())
        else:
            parts.append("## No skill active\nHelp the user pick a BMAD skill or agent "
                         "(`list_bmad_skills`, or invoke `bmad-help`).\n\n" + self.registry.catalog_text())
        state = self.workflow.state_block(session)
        if state:
            parts.append(state)
        return "\n\n".join(parts)

    # ================================================================== turns
    def start(self, *, agent: str | None = None, skill: str | None = None, message: str | dict | None = None,
              headless: bool | None = None, title: str = "", forwarded: dict[str, Any] | None = None,
              metadata: dict[str, Any] | None = None) -> TurnResult:
        """Open a session with a persona and/or a skill, and run the first turn."""
        headless = self.settings.execution.headless if headless is None else headless
        text = _as_text(message, headless)
        session = self.sessions.create(self.project_root, headless=headless,
                                       title=title or (skill or agent or (text or "")[:60]))
        if metadata:
            session.metadata.update(metadata)
        ctx = ToolContext(session=session, headless=headless)
        notes: list[str] = []
        if agent:
            persona = self.agents.activate(session, ctx, agent)
            notes.append(f"[runtime] Persona {persona['icon']} {persona['name']} activated "
                         "(customization, persistent facts and config are pre-resolved). Run the remaining "
                         "activation steps from the agent program, then greet / dispatch.")
        if skill:
            notes.append("[runtime] " + self.skills.enter(session, ctx, skill, user_input=None, forwarded=forwarded))
        if not agent and not skill:
            notes.append("[runtime] No persona or skill pre-selected.")
        user_message = "\n".join(notes) + ("\n\n" + text if text else "\n\n(no user message yet)")
        return self._run(session, ctx, user_message)

    def send(self, session_id: str, message: str | dict) -> TurnResult:
        """Deliver the user's (or planner's) next message to a session."""
        session = self.sessions.load(session_id)
        if session.status == PAUSED:
            session.status = session.metadata.pop("paused_from", ACTIVE)
        text = _as_text(message, session.headless) or ""
        self.workflow.answer(session, text)
        ctx = ToolContext(session=session, headless=session.headless)
        return self._run(session, ctx, text)

    def resume(self, session_id: str, message: str | None = None) -> TurnResult:
        """Resume a paused/interrupted session. A waiting session just returns its question."""
        session = self.sessions.load(session_id)
        if message is not None:
            return self.send(session_id, message)
        if session.status == PAUSED:
            session.status = session.metadata.pop("paused_from", ACTIVE)
        st = self.workflow.state(session)
        if session.status == WAITING and st.pending_question:
            return self._result(session, "", started=time.time(), events=[])
        if session.status in {"completed", "blocked"}:
            return self._result(session, "", started=time.time(), events=[])
        ctx = ToolContext(session=session, headless=session.headless)
        return self._run(session, ctx, "[runtime] Session resumed. Re-read the current step file if needed and "
                                       "continue exactly where the workflow stopped (do not restart).")

    def pause(self, session_id: str, note: str = "") -> dict[str, Any]:
        return self.sessions.pause(session_id, note).brief()

    def run_skill(self, skill: str, inputs: str | dict | None = None, *, agent: str | None = None,
                  headless: bool = True, max_rounds: int = 4, metadata: dict[str, Any] | None = None) -> TurnResult:
        """Run one skill to completion (headless by default) — the planner's main entry point."""
        result = self.start(agent=agent, skill=skill, message=inputs, headless=headless, metadata=metadata)
        rounds = 1
        while result.status == ACTIVE and rounds < max_rounds:     # turn budget hit → keep going
            result = self.resume(result.session_id)
            rounds += 1
        return result

    def status(self, session_id: str) -> TurnResult:
        return self._result(self.sessions.load(session_id), "", started=0, events=[])

    # ================================================================ catalog
    def list_agents(self) -> list[dict[str, Any]]:
        return [a.brief() for a in self.registry.list_agents()]

    def list_skills(self, kind: str | None = None, include_shims: bool = False) -> list[dict[str, Any]]:
        return [s.brief() for s in self.registry.list_skills(kind, include_shims)]

    def list_sessions(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.sessions.list(status)

    def health(self) -> dict[str, Any]:
        return {"bmad_root": str(self.bmad_root), "project_root": str(self.project_root),
                "registry": self.registry.snapshot.summary(), "mcp": self.tools.mcp_status,
                "mcp_tools": self.tools.mcp_tool_names(), "uv": self.scripts.uv or "not found (python fallback)",
                "model": self.settings.gomodel.model, "gateway": self.settings.gomodel.base_url}

    # =================================================== planner integration
    def planner_tools(self) -> tuple[list[dict[str, Any]], Callable[[str, dict[str, Any]], str]]:
        """OpenAI-style tool schemas + dispatcher so the Planner Agent can drive BMAD as tools."""
        S = {"type": "string"}
        schemas = [
            _fn("bmad_catalog", "List BMAD agents and skills (with phase / menu codes).", {}),
            _fn("bmad_handle_request", "Give BMAD a plain request (e.g. 'build a chess web app with React and "
                "Node'). It picks the track and the agents/skills, runs the chain headless and returns the plan "
                "with each step's status and artifacts.",
                {"request": S, "through": {"type": "string", "enum": ["plan", "ship"]}}, ["request"]),
            _fn("bmad_run_skill", "Run a BMAD skill. headless=true returns when the skill completes with its "
                "status JSON; headless=false returns at the first question.",
                {"skill": S, "inputs": {"type": "string", "description": "brief / intent / paths / change request"},
                 "agent": S, "headless": {"type": "boolean"}}, ["skill"]),
            _fn("bmad_start_agent", "Start an interactive session with a BMAD persona (pm, architect, analyst, "
                "ux-designer, dev).", {"agent": S, "message": S}, ["agent"]),
            _fn("bmad_send", "Send the next message / answer to a BMAD session.",
                {"session_id": S, "message": S}, ["session_id", "message"]),
            _fn("bmad_status", "Get a BMAD session's status, pending question and completion.",
                {"session_id": S}, ["session_id"]),
            _fn("bmad_artifacts", "List documents BMAD produced (prd, architecture, epics, story, review, memlog…).",
                {"kind": S}),
        ]

        def dispatch(name: str, args: dict[str, Any]) -> str:
            try:
                if name == "bmad_catalog":
                    out: Any = {"agents": self.list_agents(), "skills": self.list_skills()}
                elif name == "bmad_handle_request":
                    h = self.handle(args["request"], auto=True, through=args.get("through", "plan"))
                    out = {"plan": h.plan.to_dict(), "finished": h.finished, "note": h.note}
                elif name == "bmad_run_skill":
                    out = self.run_skill(args["skill"], args.get("inputs"), agent=args.get("agent"),
                                         headless=args.get("headless", True)).to_dict()
                elif name == "bmad_start_agent":
                    out = self.start(agent=args["agent"], message=args.get("message"), headless=False).to_dict()
                elif name == "bmad_send":
                    out = self.send(args["session_id"], args["message"]).to_dict()
                elif name == "bmad_status":
                    out = self.status(args["session_id"]).to_dict()
                elif name == "bmad_artifacts":
                    out = self.artifacts.as_dicts(kind=args.get("kind"))
                else:
                    out = {"error": f"unknown tool {name}"}
            except BMadAdapterError as exc:
                out = {"error": str(exc)}
            return json.dumps(out, ensure_ascii=False, default=str)

        return schemas, dispatch

    # ============================================================== internals
    def _run(self, session: Session, ctx: ToolContext, user_message: str) -> TurnResult:
        started = time.time()
        ctx.skill = self.skills.active(session)
        result = self.engine.run(ctx, self.build_system_prompt, user_message)
        self.artifacts.scan(session.session_id, (session.active_skill or {}).get("skill_id"), since=started - 1)
        self.sessions.save(session)
        return self._result(session, result.text, started=started, events=ctx.events, error=result.error)

    def _result(self, session: Session, text: str, *, started: float, events: list, error: str | None = None) -> TurnResult:
        st = self.workflow.state(session)
        changed = [a for a in self.artifacts.as_dicts(session_id=session.session_id) if a["updated"] >= started]
        message = text
        if not message:
            for m in reversed(session.messages):
                if m.get("role") == "assistant" and m.get("content"):
                    message = m["content"]
                    break
        return TurnResult(session_id=session.session_id, status=session.status, message=message or "",
                          pending_question=st.pending_question, completion=st.completion, artifacts=changed,
                          persona=(session.persona or {}).get("agent_id"),
                          skill=(session.active_skill or {}).get("skill_id"), step=st.current_step,
                          usage=dict(session.usage), error=error, events=events)


def _as_text(message: str | dict | None, headless: bool) -> str | None:
    if message is None:
        return None
    if isinstance(message, dict):
        payload = dict(message)
        if headless:
            payload.setdefault("headless", True)
        return "Invocation payload:\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```"
    if headless and message and "headless" not in message.lower():
        return message + "\n\n(headless: true — no interactive user; return the status JSON when done)"
    return message


def _fn(name: str, desc: str, props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props,
                                                            "required": required or []}}}
