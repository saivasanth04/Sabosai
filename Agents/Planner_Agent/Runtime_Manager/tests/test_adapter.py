"""End-to-end tests of the BMAD adapter against the real BMAD v6.12.4 skill files."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from conftest import BMAD_ROOT, ScriptedLLM, call, is_subagent, last_tool_result, say, tools

from bmad_adapter import BMadRuntime
from bmad_adapter.errors import PermissionDeniedError, ScriptExecutionError
from bmad_adapter.llm_client import GoModelChatClient
from bmad_adapter.mcp_client import GoModelMCPClient
from bmad_adapter.session_manager import COMPLETED, WAITING
from bmad_adapter.settings import GoModelSettings
from bmad_adapter.workflow_engine import extract_status_json


def sequence(*responses, sub=None):
    """Main-conversation responses in order; ``sub`` handles subagent calls."""
    it = iter(responses)

    def policy(messages, names):
        if is_subagent(messages) and sub:
            return sub(messages, names)
        if "working memory of a BMAD Method workflow" in messages[0]["content"]:
            return say("- user chose fast path\n- prd at prds/…")
        r = next(it)
        return r(messages, names) if callable(r) else r
    return policy


# ---------------------------------------------------------------- registry/config
def test_registry_and_install(settings_for, project):
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(sequence()), connect_mcp=False)
    h = rt.health()
    assert h["registry"]["agent_count"] == 5
    assert {a["name"] for a in rt.list_agents()} == {"Mary", "John", "Sally", "Winston", "Amelia"}
    assert (project / "_bmad" / "scripts" / "resolve_config.py").exists()
    assert (project / "_bmad" / "_config" / "bmad-help.csv").read_text().count("bmad-prd") >= 1
    v = rt.config.variables()
    assert v["user_name"] == "Ravi"
    assert v["planning_artifacts"] == str(project / "_bmad-output" / "planning-artifacts")
    cust = rt.config.customization(rt.registry.get_skill("bmad-prd").skill_dir, "workflow")
    assert cust["workflow"]["prd_output_path"] == "{planning_artifacts}/prds"


def test_custom_override_layer_respected(settings_for, project):
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(sequence()), connect_mcp=False)
    (project / "_bmad" / "custom" / "bmad-agent-pm.toml").write_text(
        '[agent]\nicon = "🧭"\npersistent_facts = ["Our org is AWS-only."]\n'
        '[[agent.menu]]\ncode = "PRD"\ndescription = "Org PRD"\nskill = "bmad-prd"\n', encoding="utf-8")
    persona = rt.agents.activate(rt.sessions.create(project, ephemeral=True), None, "john")
    assert persona["icon"] == "🧭"
    assert "Our org is AWS-only." in persona["facts"]
    assert [m["description"] for m in persona["menu"] if m["code"] == "PRD"] == ["Org PRD"]
    assert len(persona["menu"]) == 4                     # keyed merge replaced, not appended


# ------------------------------------------------------------ interactive persona
def test_interactive_pm_prd_flow(settings_for, project):
    holder = {}

    def resolve_cfg(messages, names):
        m = re.search(r"Run `(uv run [^`]*resolve_config\.py[^`]*)`", messages[0]["content"])
        assert m, "agent program with bound paths should be in the system prompt"
        holder["cmd"] = m.group(1)
        assert "{project-root}" not in m.group(1)
        return tools(call("run_script", command=m.group(1)))

    def greet(messages, names):
        assert '"user_name": "Ravi"' in last_tool_result(messages)      # BMAD's real resolver ran
        return say("📋 Hi Ravi, I'm John. | 1 PRD | 2 CE | 3 IR | 4 CC |")

    def start_prd(messages, names):
        ws = project / "_bmad-output/planning-artifacts/prds/prd-habit-app-today"
        holder["ws"] = ws
        return tools(call("run_script", command=f'uv run {project}/_bmad/scripts/memlog.py init --workspace "{ws}" --field topic=Habit'),
                     call("write_file", path=str(ws / "prd.md"), content="---\ntitle: Habit PRD\nstatus: draft\n---\n# Habit PRD\n"))

    def ask(messages, names):
        assert "exit_code: 0" in messages[-2]["content"]
        return tools(call("ask_user", question="Fast path or Coaching path?", options=["Fast path", "Coaching path"]))

    def after_answer(messages, names):
        ws = holder["ws"]
        return tools(call("run_script", command=f'uv run {project}/_bmad/scripts/memlog.py append --workspace "{ws}" --type decision --text "fast path chosen"'),
                     call("edit_file", path=str(ws / "prd.md"), old_text="status: draft", new_text="status: final"))

    def finish(messages, names):
        return tools(call("complete_workflow", status="complete", summary="PRD finalized",
                          artifacts=[str(holder["ws"] / "prd.md")]))

    llm = ScriptedLLM(sequence(
        resolve_cfg, greet,
        tools(call("invoke_skill", skill="bmad-prd", input="habit tracker PRD")),
        start_prd, ask, after_answer, finish,
        say("📋 PRD is final. Next up: `bmad-architecture` with Winston."),
    ))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)

    r1 = rt.start(agent="pm", message=None)
    assert r1.status == WAITING and r1.persona == "bmad-agent-pm" and r1.message.startswith("📋")

    r2 = rt.send(r1.session_id, "Let's write the PRD")
    assert r2.status == WAITING
    assert r2.pending_question["options"] == ["Fast path", "Coaching path"]
    assert r2.skill == "bmad-prd"
    assert "BMAD Skill: bmad-prd" in llm.calls[-1]["messages"][0]["content"]
    assert "Active BMAD persona: 📋 John" in llm.calls[-1]["messages"][0]["content"]

    r3 = rt.send(r1.session_id, "Fast path")
    assert r3.status == WAITING                               # back at John's menu
    assert r3.skill == "bmad-agent-pm"
    st = rt.workflow.state(rt.sessions.load(r1.session_id))
    assert st.completion["status"] == "complete"
    assert any(d.get("text") == "fast path chosen" for d in st.decisions)      # memlog mirrored
    memlog = (holder["ws"] / ".memlog.md").read_text(encoding="utf-8")
    assert "fast path chosen" in memlog
    prd = rt.artifacts.latest("prd")
    assert prd and prd.status == "final" and "bmad-prd" in prd.skills
    assert rt.artifacts.latest("memlog") is not None


# -------------------------------------------------------------------- headless
def test_headless_run_skill_returns_status_json(settings_for, project):
    def read_headless(messages, names):
        assert "Mode: HEADLESS" in messages[0]["content"]
        return tools(call("read_file", path="references/headless.md"))

    def write(messages, names):
        assert "# Headless Mode" in last_tool_result(messages)
        return tools(call("write_file", path="_bmad-output/planning-artifacts/prds/run1/prd.md",
                          content="---\nstatus: draft\n---\n# PRD\n- [ASSUMPTION] web only\n"))

    payload = {"status": "partial", "intent": "create",
               "prd": str(project / "_bmad-output/planning-artifacts/prds/run1/prd.md"),
               "assumptions": ["web only"], "open_questions": ["pricing?"]}
    llm = ScriptedLLM(sequence(read_headless, write, say("Done.\n```json\n" + json.dumps(payload) + "\n```")))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    r = rt.run_skill("bmad-prd", {"intent": "create", "brief": "Habit tracker for students"})
    assert r.status == COMPLETED
    assert r.completion["status"] == "partial"
    assert r.completion["result"]["open_questions"] == ["pricing?"]
    assert any(a["kind"] == "prd" for a in r.artifacts)
    assert '"headless": true' in llm.calls[0]["messages"][-1]["content"]


def test_headless_nudge_then_complete(settings_for):
    llm = ScriptedLLM(sequence(
        say("Which working mode would you like?"),                        # stalls like an IDE run would
        lambda m, n: (tools(call("complete_workflow", status="blocked", summary="no brief supplied"))
                      if "HEADLESS" in m[-1]["content"] else say("??")),
    ))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    r = rt.run_skill("bmad-product-brief", "make a brief")
    assert r.status == "blocked" and r.completion["summary"] == "no brief supplied"


def test_headless_ask_user_routes_to_planner(settings_for):
    asked = []

    def provider(q):
        asked.append(q)
        return "Coaching path"
    llm = ScriptedLLM(sequence(
        tools(call("ask_user", question="Fast or coaching?", options=["Fast", "Coaching"])),
        lambda m, n: (tools(call("complete_workflow", status="complete", summary="ok"))
                      if "Coaching path" in last_tool_result(m) else say("bad")),
    ))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False, answer_provider=provider)
    r = rt.run_skill("bmad-prd", "x")
    assert r.status == COMPLETED and asked[0]["skill"] == "bmad-prd"


# ------------------------------------------------------------------ subagents
def test_parallel_reviewer_subagents(settings_for, project):
    def sub(messages, names):
        brief = messages[-1]["content"] if messages[-1]["role"] == "user" else ""
        if messages[-1]["role"] == "tool":
            return say("verdict: pass; findings: 2; file written")
        assert "write_file" in names and "ask_user" not in names
        out = re.search(r"Write your full output to `([^`]+)`", brief).group(1)
        return tools(call("write_file", path=out, content="# review\nok"))

    llm = ScriptedLLM(sequence(
        tools(call("spawn_subagents", tasks=[
            {"brief": "Review prd.md for structure", "profile": "write", "label": "structure",
             "output_file": "_bmad-output/planning-artifacts/prds/r/review-structure.md"},
            {"brief": "Review prd.md for prose", "profile": "write", "skill": "bmad-review",
             "output_file": "_bmad-output/planning-artifacts/prds/r/review-prose.md"}])),
        lambda m, n: tools(call("complete_workflow", status="complete", summary=last_tool_result(m)[:100])),
        sub=sub))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    r = rt.run_skill("bmad-prd", "validate")
    msgs = rt.sessions.load(r.session_id).messages
    recs = json.loads(next(m["content"] for m in msgs if m["role"] == "tool" and m["content"].startswith("[")))
    assert [x["status"] for x in recs] == [COMPLETED, COMPLETED]
    assert recs[1]["skill"] == "bmad-review"
    assert (project / "_bmad-output/planning-artifacts/prds/r/review-prose.md").exists()
    assert len(list((project / "_bmad/runtime/subagents").glob("sub-*.json"))) == 2
    assert {a.kind for a in rt.artifacts.list()} >= {"review"}


def test_research_subagent_is_read_only(settings_for):
    seen = {}

    def sub(messages, names):
        seen["names"] = names
        return say("digest")
    llm = ScriptedLLM(sequence(tools(call("spawn_subagent", brief="research comparables")),
                               say("done"), sub=sub))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    rt.start(skill="bmad-prd", message="go")
    assert "write_file" not in seen["names"] and "read_file" in seen["names"]


# ------------------------------------------------------ render skills / shims
def test_render_based_build_skill_tracks_steps(settings_for, project):
    def read_step1(messages, names):
        m = re.search(r"Read fully and follow: `([^`]+step-01-clarify-and-route\.md)`", messages[0]["content"])
        assert m, "rendered workflow.md should be the program"
        return tools(call("read_file", path=m.group(1)))

    def jump(messages, names):
        res = last_tool_result(messages)
        assert "Step 1: Clarify" in res and "{{." not in res          # render tokens resolved
        step3 = re.search(r"`([^`]+step-03-implement\.md)`", res).group(1)
        return tools(call("read_file", path=step3))

    def halt(messages, names):
        assert "BMAD forbids skipping steps" in last_tool_result(messages)
        return tools(call("ask_user", question="Resume spec or New?"))

    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(sequence(read_step1, jump, halt)), connect_mcp=False)
    r = rt.start(skill="bmad-build", message="add dark mode")
    assert r.status == WAITING
    assert Path(r.step).name == "step-03-implement.md"
    assert "/_bmad/render/bmad-build/" in r.step.replace("\\", "/")


def test_shim_tail_calls_target(settings_for):
    llm = ScriptedLLM(sequence(
        tools(call("invoke_skill", skill="bmad-create-prd")),
        tools(call("invoke_skill", skill="bmad-prd", forwarded={"intent": "create"})),
        say("Hi! Tell me about the product?"),
    ))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    r = rt.start(message="create a prd")
    s = rt.sessions.load(r.session_id)
    assert [e["skill_id"] for e in s.skill_stack] == ["bmad-prd"]
    assert s.skill_stack[0]["forwarded"] == {"intent": "create"}


# ----------------------------------------------------------------- permissions
def test_permissions(settings_for, project):
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(sequence()), connect_mcp=False)
    with pytest.raises(PermissionDeniedError):
        rt.permissions.check_write(Path(BMAD_ROOT) / "src" / "evil.md")
    with pytest.raises(PermissionDeniedError):
        rt.permissions.check_write(project / "_bmad" / "scripts" / "memlog.py")
    with pytest.raises(PermissionDeniedError):
        rt.permissions.check_write("../outside.md")
    with pytest.raises(PermissionDeniedError):
        rt.permissions.check_read("/etc/passwd")
    rogue = project / "rogue.py"
    rogue.write_text("print(1)")
    with pytest.raises(PermissionDeniedError):
        rt.scripts.run(rogue)
    with pytest.raises(ScriptExecutionError):
        rt.scripts.parse_command("rm -rf /")
    ro = BMadRuntime(settings_for(permissions={"mode": "readonly"}), llm=ScriptedLLM(sequence()), connect_mcp=False)
    with pytest.raises(PermissionDeniedError):
        ro.permissions.check_write(project / "x.md")

    llm = ScriptedLLM(sequence(tools(call("write_file", path=str(Path(BMAD_ROOT) / "x.md"), content="no")),
                               say("ok")))
    rt2 = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    rt2.start(message="try")
    assert "PERMISSION DENIED" in llm.calls[-1]["messages"][-1]["content"]


def test_ask_mode_uses_approval_callback(settings_for, project):
    decisions = []
    rt = BMadRuntime(settings_for(permissions={"mode": "ask"}), llm=ScriptedLLM(sequence()), connect_mcp=False,
                     approval_callback=lambda req: decisions.append(req.category) or False)
    with pytest.raises(PermissionDeniedError):
        rt.permissions.check_write(project / "doc.md")
    assert decisions == ["write"]


# ------------------------------------------------------------ sessions/context
def test_pause_and_resume_across_runtime_instances(settings_for):
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(sequence(
        tools(call("ask_user", question="What are we building?")))), connect_mcp=False)
    r = rt.start(skill="bmad-product-brief", message="new product")
    rt.pause(r.session_id)

    llm2 = ScriptedLLM(sequence(lambda m, n: (say("Great — a habit app. Who is it for?")
                                              if m[-1]["content"] == "A habit app" else say("wrong"))))
    rt2 = BMadRuntime(settings_for(), llm=llm2, connect_mcp=False)
    again = rt2.resume(r.session_id)
    assert again.status == WAITING and again.pending_question["question"] == "What are we building?"
    assert llm2.calls == []                                   # no model call needed to re-surface the question
    r2 = rt2.send(r.session_id, "A habit app")
    assert r2.message.startswith("Great") and r2.skill == "bmad-product-brief"
    assert "BMAD Skill: bmad-product-brief" in llm2.calls[0]["messages"][0]["content"]


def test_context_compaction(settings_for):
    big = "x" * 9000
    responses = [tools(call("list_dir", path=".")) for _ in range(6)] + [say("done")]
    llm = ScriptedLLM(sequence(*responses))
    rt = BMadRuntime(settings_for(execution={"context_token_budget": 6000, "compaction_keep_recent": 4}),
                     llm=llm, connect_mcp=False)
    r = rt.start(skill="bmad-brainstorming", message=big)
    s = rt.sessions.load(r.session_id)
    assert "fast path" in s.memory                             # summary produced by the summariser call
    roles = [m["role"] for m in s.messages]
    assert roles[0] != "tool"                                 # never starts with an orphan tool result


# ---------------------------------------------------------- GoModel clients
def test_gomodel_chat_client(fake_gomodel):
    c = GoModelChatClient(GoModelSettings(base_url=fake_gomodel.base_url, model="m1"))
    r = c.chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "x", "parameters": {}}}])
    assert r.tool_calls[0].name == "duckduckgo_search" and r.tool_calls[0].arguments == {"query": "x"}
    assert fake_gomodel.requests[-1]["body"]["model"] == "m1"


def test_gomodel_mcp_tools_through_runtime(settings_for, fake_gomodel, monkeypatch):
    monkeypatch.setenv("GOMODEL_API_KEY", "sk-test")
    s = settings_for(gomodel={"mcp_enabled": True, "base_url": fake_gomodel.base_url, "mcp_servers": ["duckduckgo"]})
    llm = ScriptedLLM(sequence(tools(call("duckduckgo_search", query="habit apps 2026")),
                               lambda m, n: say("found: " + last_tool_result(m))))
    rt = BMadRuntime(s, llm=llm, mcp_client=GoModelMCPClient(s.gomodel))
    assert rt.tools.mcp_tool_names() == ["crawl4ai_crawl_url", "duckduckgo_search"]   # paginated list
    r = rt.start(skill="bmad-deep-recon", message="research habit apps")
    assert "results for" in r.message and "habit apps 2026" in r.message               # SSE response parsed
    mcp_reqs = [q for q in fake_gomodel.requests if q["path"] == "/mcp"]
    hdrs = {k.lower(): v for k, v in mcp_reqs[-1]["headers"].items()}     # HTTP headers are case-insensitive
    assert hdrs["authorization"] == "Bearer sk-test"
    assert hdrs["mcp-session-id"] == "sess-123"
    assert hdrs["x-mcp-servers"] == "duckduckgo"
    assert "`duckduckgo_search`" in llm.calls[0]["messages"][0]["content"]


def test_mcp_unavailable_degrades(settings_for):
    s = settings_for(gomodel={"mcp_enabled": True, "base_url": "http://127.0.0.1:9"})
    rt = BMadRuntime(s, llm=ScriptedLLM(sequence()))
    assert rt.tools.mcp_status.startswith("unavailable")


# -------------------------------------------------------------- planner API
def test_planner_tools(settings_for):
    llm = ScriptedLLM(sequence(tools(call("complete_workflow", status="complete", summary="ok"))))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    schemas, dispatch = rt.planner_tools()
    assert {s["function"]["name"] for s in schemas} >= {"bmad_run_skill", "bmad_send", "bmad_catalog"}
    cat = json.loads(dispatch("bmad_catalog", {}))
    assert any(s["id"] == "bmad-architecture" for s in cat["skills"])
    out = json.loads(dispatch("bmad_run_skill", {"skill": "PRD", "inputs": "brief"}))   # menu code lookup
    assert out["status"] == COMPLETED and out["completion"]["status"] == "complete"


def test_extract_status_json():
    assert extract_status_json('x\n{"status": "complete", "prd": "a.md"}')["prd"] == "a.md"
    assert extract_status_json("```json\n{\"status\":\"Blocked\",\"reason\":\"r\"}\n```")["status"] == "blocked"
    assert extract_status_json('{"foo": 1}') is None


def test_split_command_windows_and_posix():
    from bmad_adapter.script_executor import split_command
    win = (r'uv run C:\Projects\NTG\app\_bmad\scripts\memlog.py append --workspace "C:\Projects\NTG\app\out x" '
           r'--type decision --text "fast path chosen" --field topic="Habit app"')
    assert split_command(win) == ["uv", "run", r"C:\Projects\NTG\app\_bmad\scripts\memlog.py", "append",
                                  "--workspace", r"C:\Projects\NTG\app\out x", "--type", "decision",
                                  "--text", "fast path chosen", "--field", "topic=Habit app"]
    assert split_command("python3 /a/b.py --text 'it is \"ok\"' ''") == ["python3", "/a/b.py", "--text", 'it is "ok"', ""]


# ------------------------------------------------------------------ orchestrator
def _router_policy(route_json, step_policy):
    def policy(messages, names):
        if "BMAD Method router" in messages[0]["content"]:
            return say(route_json if isinstance(route_json, str) else "```json\n" + json.dumps(route_json) + "\n```")
        return step_policy(messages, names)
    return policy


CHESS = "I want to build a chess web application using React & Node.js"
ROUTE = {"track": "method", "scale": "large", "reason": "new product",
         "steps": [{"skill": "bmad-prd", "agent": "bmad-agent-pm", "why": "requirements"},
                   {"skill": "bmad-architecture", "agent": "bmad-agent-pm", "why": "spine"},   # wrong owner
                   {"skill": "bmad-ux", "agent": None, "why": "screens"},
                   {"skill": "bmad-not-a-skill", "agent": None, "why": "x"},
                   {"skill": "bmad-build", "agent": "bmad-agent-dev", "why": "code"}]}


def test_router_llm_plan(settings_for):
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(_router_policy(ROUTE, lambda m, n: say("x"))), connect_mcp=False)
    plan = rt.orchestrator.route(CHESS)
    assert plan.router == "llm" and plan.track == "method"
    assert [(s.skill, s.agent) for s in plan.steps] == [
        ("bmad-prd", "bmad-agent-pm"), ("bmad-ux", "bmad-agent-ux-designer"),
        ("bmad-architecture", "bmad-agent-architect")]          # ordered, owner fixed, junk + build dropped
    ship = rt.orchestrator.route(CHESS, through="ship")
    assert ship.steps[-1].skill == "bmad-build" and ship.steps[-1].agent == "bmad-agent-dev"


def test_router_heuristic_fallback(settings_for, project):
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(_router_policy("no json here", lambda m, n: say("x"))),
                     connect_mcp=False)
    plan = rt.orchestrator.route(CHESS)
    assert plan.router == "heuristic"
    assert [s.skill for s in plan.steps] == ["bmad-prd", "bmad-ux", "bmad-architecture",
                                             "bmad-create-epics-and-stories", "bmad-sprint-planning"]
    (project / "package.json").write_text("{}")
    quick = rt.orchestrator.route("fix the bug in the login form")
    assert quick.track == "quick" and quick.steps[0].skill == "bmad-build" and quick.steps[0].agent == "bmad-agent-dev"
    assert rt.orchestrator.route("research the chess engine market").steps[0].skill == "bmad-deep-recon"


def test_handle_auto_runs_chain(settings_for, project):
    route = {"track": "method", "scale": "large", "reason": "new",
             "steps": [{"skill": "bmad-prd", "agent": "bmad-agent-pm", "why": "req"},
                       {"skill": "bmad-architecture", "agent": "bmad-agent-architect", "why": "spine"}]}
    briefs = []

    def step(messages, names):
        if messages[-1]["role"] == "user":
            briefs.append(messages[-1]["content"])
            sk = "prd" if "bmad-prd:" in messages[-1]["content"] else "architecture"
            return tools(call("write_file", path=f"_bmad-output/planning-artifacts/{sk}.md",
                              content=f"---\nstatus: final\n---\n# {sk}\n"))
        return tools(call("complete_workflow", status="complete", summary="done"))
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(_router_policy(route, step)), connect_mcp=False)
    h = rt.handle(CHESS, auto=True)
    assert h.finished and [s.status for s in h.plan.steps] == ["completed", "completed"]
    assert "planning-artifacts/prd.md" in "".join(briefs[1].split())          # prior artifacts handed on
    assert "Persona" in briefs[1] and "Winston" in briefs[1]
    saved = rt.orchestrator.latest()
    assert saved.plan_id == h.plan.plan_id and saved.steps[1].artifacts


def test_handle_interactive_advances(settings_for):
    route = {"track": "method", "scale": "large", "reason": "new",
             "steps": [{"skill": "bmad-prd", "agent": "bmad-agent-pm", "why": "req"},
                       {"skill": "bmad-architecture", "agent": "bmad-agent-architect", "why": "spine"}]}
    seq = iter([tools(call("ask_user", question="Fast or coaching?")),
                tools(call("complete_workflow", status="complete", summary="PRD done")),
                say("📋 PRD done."),
                say("🏗️ Hi, I'm Winston. Let's shape the architecture?")])
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(_router_policy(route, lambda m, n: next(seq))),
                     connect_mcp=False)
    h = rt.handle(CHESS)
    assert h.turn.persona == "bmad-agent-pm" and h.turn.status == WAITING
    t = rt.send(h.turn.session_id, "Fast")
    h2 = rt.continue_plan(t)
    assert h2.turn.session_id != t.session_id
    assert h2.turn.persona == "bmad-agent-architect" and h2.turn.skill == "bmad-architecture"
    assert h2.plan.steps[0].status == "completed" and h2.plan.steps[1].status == "running"


# ------------------------------------------------------------- command executor
def test_command_executor_policy(settings_for, project):
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(sequence()), connect_mcp=False)
    cx = rt.commands
    assert cx.run("node --version").ok
    for bad, exc in [("rm -rf /", PermissionDeniedError), ("git push origin main", PermissionDeniedError),
                     ("npm publish", PermissionDeniedError), ("npm install -g left-pad", PermissionDeniedError)]:
        with pytest.raises(exc):
            cx.run(bad)
    with pytest.raises(ScriptExecutionError):
        cx.run("npm install && npm test")
    with pytest.raises(ScriptExecutionError):
        cx.run("npm run dev")                                   # long-running needs background
    with pytest.raises(PermissionDeniedError):
        cx.run("node --version", cwd=str(Path(BMAD_ROOT)))     # cwd outside project
    (project / "srv.js").write_text("import x from './missing.js'\n")
    (project / "ok.js").write_text("setInterval(() => {}, 1000)\n")
    assert not cx.smoke_start(["node", "srv.js"], seconds=4).ok      # crash detected
    assert cx.smoke_start(["node", "ok.js"], seconds=2).ok
    bp = cx.start_background("node ok.js")
    assert "running" in cx.status(bp.proc_id)
    cx.stop(bp.proc_id)


def test_run_command_tool_and_readonly(settings_for):
    llm = ScriptedLLM(sequence(tools(call("run_command", command="node -e console.log(40+2)")),
                               lambda m, n: say(last_tool_result(m))))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    r = rt.start(skill="bmad-build", message="x")
    assert "42" in r.message and "exit_code: 0" in r.message
    ro = BMadRuntime(settings_for(permissions={"mode": "readonly"}), llm=ScriptedLLM(sequence()), connect_mcp=False)
    with pytest.raises(PermissionDeniedError):
        ro.commands.run("node --version")


# ------------------------------------------------------------- drift prevention
def test_orchestrated_session_guards(settings_for):
    llm = ScriptedLLM(sequence(
        tools(call("invoke_skill", skill="bmad-prfaq")),                              # wander off → refused
        tools(call("activate_agent", agent="dev")),                                   # persona switch → refused
        tools(call("invoke_skill", skill="bmad-review", input="review brief.md")),    # → isolated subagent
        say("done?"),
        sub=lambda m, n: say("review verdict: ok")))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    r = rt.start(agent="analyst", skill="bmad-product-brief", message="go",
                 metadata={"plan_step_skill": "bmad-product-brief", "plan_next": "`bmad-prd` with bmad-agent-pm"})
    msgs = [m["content"] for m in rt.sessions.load(r.session_id).messages if m["role"] == "tool"]
    assert "separate BMAD phase" in msgs[0] and "bmad-prd" in msgs[0]
    assert "orchestrated step" in msgs[1]
    assert json.loads(msgs[2])["skill"] == "bmad-review"                             # ran isolated
    s = rt.sessions.load(r.session_id)
    assert [e["skill_id"] for e in s.skill_stack] == ["bmad-agent-analyst", "bmad-product-brief"]


def test_reinvoking_same_skill_does_not_stack(settings_for):
    llm = ScriptedLLM(sequence(tools(call("invoke_skill", skill="bmad-build", input="story 1.2")),
                               tools(call("invoke_skill", skill="bmad-build", input="story 1.3")), say("ok")))
    rt = BMadRuntime(settings_for(), llm=llm, connect_mcp=False)
    r = rt.start(agent="dev", skill="bmad-build", message="story 1.1")
    assert [e["skill_id"] for e in rt.sessions.load(r.session_id).skill_stack] == ["bmad-agent-dev", "bmad-build"]


def test_sub_skill_completion_does_not_advance_plan(settings_for):
    route = {"track": "method", "scale": "large", "reason": "r",
             "steps": [{"skill": "bmad-product-brief", "agent": "bmad-agent-analyst", "why": "b"},
                       {"skill": "bmad-prd", "agent": "bmad-agent-pm", "why": "p"}]}
    seq = iter([tools(call("invoke_skill", skill="bmad-advanced-elicitation")),       # inline sub-skill
                tools(call("complete_workflow", status="complete", summary="elicitation done")),
                say("Back in the brief — anything else?")])
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(_router_policy(route, lambda m, n: next(seq))),
                     connect_mcp=False)
    h = rt.handle(CHESS)
    h2 = rt.continue_plan(h.turn)
    assert h2.turn.session_id == h.turn.session_id and h2.plan.steps[0].status == "running"


# ------------------------------------------------------- per-story build + verify
def _sprint(project, keys):
    d = project / "_bmad-output" / "implementation-artifacts"
    d.mkdir(parents=True, exist_ok=True)
    body = "development_status:\n  epic-1: in-progress\n" + "".join(f"  {k}: {v}\n" for k, v in keys)
    (d / "sprint-status.yaml").write_text(body)


def test_build_expands_per_story_and_verifies(settings_for, project):
    _sprint(project, [("1-1-create-room", "done"), ("1-2-join-room", "backlog"), ("2-1-moves", "ready-for-dev")])
    route = {"track": "quick", "scale": "small", "reason": "r",
             "steps": [{"skill": "bmad-build", "agent": "bmad-agent-dev", "why": "code"}]}
    stories, fixes = [], []

    def step(messages, names):
        last = messages[-1]
        if last["role"] == "user" and "[orchestrator]" in last["content"]:
            fixes.append(last["content"])
            return tools(call("write_file", path="server/lib.js", content="export default 1\n"),
                         call("complete_workflow", status="complete", summary="fixed"))
        if last["role"] == "user":
            stories.append(re.search(r"Implement ONLY story `([^`]+)`", last["content"]).group(1))
            files = [call("write_file", path="server/package.json", content=json.dumps(
                         {"name": "srv", "type": "module", "scripts": {"start": "node index.js"}})),
                     call("write_file", path="server/index.js",
                          content="import x from './lib.js'\nsetInterval(() => {}, 1000)\n")]
            return tools(*files, call("complete_workflow", status="complete", summary="story done"))
        return say("ok")
    rt = BMadRuntime(settings_for(), llm=ScriptedLLM(_router_policy(route, step)), connect_mcp=False)
    h = rt.handle("fix things", auto=True, through="ship")
    assert stories == ["1-2-join-room", "2-1-moves"]                       # done story skipped, one session each
    assert [s.story for s in h.plan.steps] == ["1-2-join-room", "2-1-moves"]
    assert len(fixes) == 1
    assert "start-up (index.js)" in fixes[0] and "lib.js" in fixes[0]          # real node error sent back
    assert all(s.status == "completed" and s.verification["ok"] for s in h.plan.steps)
    checks = {c["check"].split(" ")[0] for c in h.plan.steps[-1].verification["checks"]}
    assert {"install", "start-up"} <= checks


# ------------------------------------------------- GoModel filesystem MCP backend
def _fs_mcp(srv, roots, allowed):
    """Turn the fake gateway into GoModel + @modelcontextprotocol/server-filesystem (namespaced filesystem_*)."""
    import difflib
    roots = [str(Path(r).resolve()) for r in roots]

    def ok(p):
        rp = str(Path(p).resolve())
        if not any(rp == r or rp.startswith(r + os.sep) for r in roots):
            raise PermissionError(f"Access denied - path outside allowed directories: {p}")
        return Path(rp)

    def tool(name, a):
        if name == "list_allowed_directories":
            return "Allowed directories:\n" + "\n".join(roots)
        if name == "read_text_file":
            lines = ok(a["path"]).read_text().splitlines()
            if "head" in a:
                lines = lines[:a["head"]]
            if "tail" in a:
                lines = lines[-a["tail"]:]
            return "\n".join(lines)
        if name == "read_multiple_files":
            return "\n---\n".join(f"{p}:\n{ok(p).read_text()}\n" for p in a["paths"])
        if name == "list_directory_with_sizes":
            d = ok(a["path"])
            return "\n".join(f"[DIR] {c.name}" if c.is_dir() else f"[FILE] {c.name:<30} {c.stat().st_size:>8} B"
                             for c in sorted(d.iterdir())) + "\n\nTotal: x"
        if name == "directory_tree":
            def walk(d):
                return [{"name": c.name, "type": "directory", "children": walk(c)} if c.is_dir()
                        else {"name": c.name, "type": "file"} for c in sorted(d.iterdir())
                        if c.name not in (a.get("excludePatterns") or [])]
            return json.dumps(walk(ok(a["path"])))
        if name == "search_files":
            d = ok(a["path"])
            hits = [str(c) for c in d.glob(a["pattern"])
                    if not any(x in c.parts for x in (a.get("excludePatterns") or []))]
            return "\n".join(hits) or "No matches found"
        if name == "get_file_info":
            return f"size: {ok(a['path']).stat().st_size}\nisFile: true"
        if name == "write_file":
            p = ok(a["path"])
            if not p.parent.is_dir():
                raise FileNotFoundError(f"ENOENT: parent directory does not exist: {p.parent}")
            p.write_text(a["content"])
            return f"Successfully wrote to {a['path']}"
        if name == "append_file":
            with ok(a["path"]).open("a") as fh:
                fh.write(a["content"])
            return f"Successfully appended to {a['path']}"
        if name == "edit_file":
            p = ok(a["path"])
            old = text = p.read_text()
            for e in a["edits"]:
                if e["oldText"] not in text:
                    raise ValueError(f"Could not find exact match for edit:\n{e['oldText']}")
                text = text.replace(e["oldText"], e["newText"], 1)
            if not a.get("dryRun"):
                p.write_text(text)
            return "```diff\n" + "".join(difflib.unified_diff(old.splitlines(True), text.splitlines(True))) + "```"
        if name == "create_directory":
            ok(a["path"]).mkdir(parents=True, exist_ok=True)
            return f"Successfully created directory {a['path']}"
        if name == "move_file":
            ok(a["source"]).rename(ok(a["destination"]))
            return "Successfully moved"
        if name == "copy_file":
            import shutil
            shutil.copy2(ok(a["source"]), ok(a["destination"]))
            return "Successfully copied"
        raise ValueError(name)

    def mcp_reply(body):
        m = body["method"]
        if m == "initialize":
            return {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
        if m == "tools/list":
            return {"tools": [{"name": f"filesystem_{t}", "description": t,
                               "inputSchema": {"type": "object", "properties": {}}} for t in allowed]}
        if m == "tools/call":
            name = body["params"]["name"].removeprefix("filesystem_")
            if name not in allowed:
                return {"content": [{"type": "text", "text": f"tool {name} not allowed"}], "isError": True}
            try:
                return {"content": [{"type": "text", "text": tool(name, body["params"]["arguments"])}]}
            except Exception as exc:  # noqa: BLE001
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {}
    srv.mcp_reply = mcp_reply
    srv.sse = False


FS_ALL = ["list_allowed_directories", "read_text_file", "read_multiple_files", "list_directory",
          "list_directory_with_sizes", "directory_tree", "search_files", "get_file_info", "write_file",
          "append_file", "edit_file", "create_directory", "move_file", "copy_file"]
FS_READONLY = ["list_allowed_directories", "list_directory", "directory_tree", "search_files", "get_file_info",
               "read_text_file", "read_multiple_files"]       # = the current config.yaml whitelist


def _fs_calls(srv):
    return [q["body"]["params"]["name"] for q in srv.requests
            if q["path"] == "/mcp" and q["body"].get("method") == "tools/call"]


def test_file_tools_route_through_gomodel_filesystem(settings_for, project, fake_gomodel):
    _fs_mcp(fake_gomodel, [project, BMAD_ROOT], FS_ALL)
    seen = {}

    def first(m, names):
        seen["names"] = names
        return tools(call("write_file", path="docs/notes/plan.md", content="---\nstatus: draft\n---\n# Plan\nA\n"),
                     call("append_file", path="docs/notes/plan.md", content="B\n"),
                     call("edit_file", path="docs/notes/plan.md", edits=[{"oldText": "status: draft",
                                                                         "newText": "status: final"}]),
                     call("read_file", path="docs/notes/plan.md", head=3),
                     call("list_dir", path="docs/notes"),
                     call("search_files", glob="**/*.md", contains="Plan"),
                     call("copy_file", source="docs/notes/plan.md", destination="docs/copy.md"),
                     call("read_file", path="references/headless.md"))          # BMAD skill file, via MCP too
    llm = ScriptedLLM(sequence(first, lambda m, n: say("done")))
    s = settings_for(gomodel={"mcp_enabled": True, "base_url": fake_gomodel.base_url})
    rt = BMadRuntime(s, llm=llm, mcp_client=GoModelMCPClient(s.gomodel))
    assert not any(n.startswith("filesystem_") for n in rt.tools.mcp_tool_names())   # raw tools hidden
    rt.start(skill="bmad-prd", message="go")
    assert not any(n.startswith("filesystem_") for n in seen["names"])
    assert {"create_directory", "move_file", "copy_file", "directory_tree", "file_info"} <= set(seen["names"])
    results = [m["content"] for m in llm.calls[-1]["messages"] if m["role"] == "tool"]
    assert all("[mcp]" in r for r in results[:7]), results[:7]
    assert "status: final" in results[3] and "B" not in results[3]               # head=3 honoured by the server
    assert "# Headless Mode" in results[7] and "{project-root}" not in results[7]   # token binding kept
    calls = _fs_calls(fake_gomodel)
    for t in ("write_file", "append_file", "edit_file", "read_text_file", "list_directory_with_sizes",
              "search_files", "copy_file", "create_directory"):
        assert f"filesystem_{t}" in calls, t
    assert (project / "docs/notes/plan.md").read_text().count("status: final") == 1
    art = rt.artifacts.as_dicts()
    assert any(a["path"] == "docs/notes/plan.md" and a["status"] == "final" for a in art)   # tracking kept
    h = rt.health()["filesystem"]
    assert h["writes_via"] == "mcp" and h["calls"]["local"] == 0


def test_readonly_whitelist_falls_back_for_writes(settings_for, project, fake_gomodel):
    _fs_mcp(fake_gomodel, [project, BMAD_ROOT], FS_READONLY)
    llm = ScriptedLLM(sequence(tools(call("write_file", path="a.md", content="x"), call("read_file", path=str(project / "a.md"))),
                               say("ok")))
    s = settings_for(gomodel={"mcp_enabled": True, "base_url": fake_gomodel.base_url})
    rt = BMadRuntime(s, llm=llm, mcp_client=GoModelMCPClient(s.gomodel))
    rt.start(message="x")
    res = [m["content"] for m in llm.calls[-1]["messages"] if m["role"] == "tool"]
    assert "[local]" in res[0] and "[mcp]" in res[1]
    h = rt.health()["filesystem"]
    assert h["writes_via"].startswith("local") and any("allowed_tools" in f for f in h["fallbacks"])
    assert "filesystem_write_file" not in _fs_calls(fake_gomodel)


def test_strict_mcp_backend_and_permissions(settings_for, project, fake_gomodel):
    _fs_mcp(fake_gomodel, [project, BMAD_ROOT], FS_READONLY)
    llm = ScriptedLLM(sequence(tools(call("write_file", path="a.md", content="x"),
                                     call("write_file", path=str(Path(BMAD_ROOT) / "evil.md"), content="x"),
                                     call("edit_file", path=str(Path(BMAD_ROOT) / "README.md"), old_text="BMad",
                                          new_text="X")),
                               say("ok")))
    s = settings_for(gomodel={"mcp_enabled": True, "base_url": fake_gomodel.base_url},
                     filesystem={"backend": "mcp"})
    rt = BMadRuntime(s, llm=llm, mcp_client=GoModelMCPClient(s.gomodel))
    rt.start(message="x")
    res = [m["content"] for m in llm.calls[-1]["messages"] if m["role"] == "tool"]
    assert "backend is 'mcp'" in res[0] and "allowed_tools" in res[0]
    assert "PERMISSION DENIED" in res[1] and "PERMISSION DENIED" in res[2]       # blocked before reaching MCP
    assert not (project / "a.md").exists()


def test_mcp_error_surfaces_and_outside_roots_falls_back(settings_for, project, fake_gomodel, tmp_path):
    _fs_mcp(fake_gomodel, [project], FS_ALL)                     # BMAD clone NOT in MCP roots
    (project / "n.md").write_text("hello\n")
    llm = ScriptedLLM(sequence(tools(call("edit_file", path="n.md", edits=[{"oldText": "nope", "newText": "x"}]),
                                     call("read_file", path="references/headless.md")), say("ok")))
    s = settings_for(gomodel={"mcp_enabled": True, "base_url": fake_gomodel.base_url})
    rt = BMadRuntime(s, llm=llm, mcp_client=GoModelMCPClient(s.gomodel))
    rt.start(skill="bmad-prd", message="x")
    res = [m["content"] for m in llm.calls[-1]["messages"] if m["role"] == "tool"]
    assert res[0].startswith("ERROR: filesystem_edit_file: Could not find exact match")
    assert "[local]" in res[1] and "# Headless Mode" in res[1]
    assert any("outside" in f for f in rt.fs.describe()["fallbacks"])
