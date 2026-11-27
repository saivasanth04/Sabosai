"""Built-in runtime tools exposed to the LLM (the "IDE" surface BMAD skills expect)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import BMadLoaderError, ToolError
from .skill_executor import ISOLATE_WHEN_NESTED
from .tool_manager import Tool, ToolContext

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import BMadRuntime


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}


def register_builtin_tools(rt: "BMadRuntime") -> None:
    tm = rt.tools

    # ------------------------------------------------------------ filesystem
    def read_file(args: dict[str, Any], ctx: ToolContext) -> str:
        path = args.get("path") or ""
        try:
            res = rt.loader.read_resource(path, ctx.skill)
            p, content = res.path, res.content
        except BMadLoaderError:
            p = rt.permissions.check_read(path)
            if not p.is_file():
                raise ToolError(f"file not found: {p}")
            content = p.read_text(encoding="utf-8", errors="replace").replace("{project-root}", str(rt.project_root))
        else:
            rt.permissions.check_read(p)
        warning = rt.workflow.on_file_read(ctx.session, p)
        start, limit = int(args.get("offset") or 0), args.get("max_lines")
        if start or limit:
            lines = content.splitlines()
            content = "\n".join(lines[start: start + int(limit) if limit else None])
        header = f"# {p}\n"
        return header + content + (f"\n\n{warning}" if warning else "")

    def list_dir(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_read(args.get("path") or str(rt.project_root))
        if not p.is_dir():
            raise ToolError(f"not a directory: {p}")
        recursive = bool(args.get("recursive"))
        pattern = args.get("pattern") or "*"
        it = p.rglob(pattern) if recursive else p.glob(pattern)
        rows = []
        for i, child in enumerate(sorted(it)):
            if i >= 500:
                rows.append("…(truncated at 500 entries)")
                break
            if any(part in {".git", "node_modules", "__pycache__"} for part in child.parts):
                continue
            kind = "dir " if child.is_dir() else f"{child.stat().st_size:>8} B"
            rows.append(f"{kind}  {child.relative_to(p).as_posix()}")
        return f"# {p}\n" + ("\n".join(rows) or "(empty)")

    def search_files(args: dict[str, Any], ctx: ToolContext) -> str:
        root = rt.permissions.check_read(args.get("root") or str(rt.project_root))
        glob_pat = args.get("glob") or "*"
        needle = (args.get("contains") or "").lower()
        hits = []
        for p in root.rglob(glob_pat):
            if not p.is_file() or any(x in p.parts for x in (".git", "node_modules", "__pycache__")):
                continue
            if needle:
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                idx = text.lower().find(needle)
                if idx == -1:
                    continue
                line_no = text.count("\n", 0, idx) + 1
                hits.append(f"{p}:{line_no}: {text[max(0, idx - 60): idx + 100].replace(chr(10), ' ')}")
            else:
                hits.append(str(p))
            if len(hits) >= 200:
                break
        return "\n".join(hits) or "no matches"

    def write_file(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_write(args["path"])
        if args.get("mode") == "create" and p.exists():
            raise ToolError(f"{p} already exists (mode=create)")
        p.parent.mkdir(parents=True, exist_ok=True)
        content = args.get("content", "")
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)
        art = rt.artifacts.record(p, action="write", session_id=ctx.session.session_id,
                                  skill_id=(ctx.session.active_skill or {}).get("skill_id"))
        _track(ctx, p)
        return f"wrote {p} ({len(content)} chars)" + (f" [artifact: {art.kind}, status={art.status}]" if art else "")

    def append_file(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_write(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(args.get("content", ""))
        rt.artifacts.record(p, action="append", session_id=ctx.session.session_id,
                            skill_id=(ctx.session.active_skill or {}).get("skill_id"))
        _track(ctx, p)
        return f"appended {len(args.get('content', ''))} chars to {p}"

    def edit_file(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_write(args["path"])
        if not p.is_file():
            raise ToolError(f"file not found: {p}")
        text = p.read_text(encoding="utf-8")
        old, new = args.get("old_text", ""), args.get("new_text", "")
        count = text.count(old) if old else 0
        if count == 0:
            raise ToolError("old_text not found — re-read the file and copy the exact text")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_text occurs {count} times; add context or set replace_all=true")
        text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        p.write_text(text, encoding="utf-8")
        rt.artifacts.record(p, action="edit", session_id=ctx.session.session_id,
                            skill_id=(ctx.session.active_skill or {}).get("skill_id"))
        _track(ctx, p)
        return f"edited {p} ({count if args.get('replace_all') else 1} replacement(s))"

    # ---------------------------------------------------------------- scripts
    def run_script(args: dict[str, Any], ctx: ToolContext) -> str:
        if args.get("command"):
            res = rt.scripts.run_command(args["command"], stdin=args.get("stdin"))
        elif args.get("script"):
            res = rt.scripts.run(rt.permissions.resolve(args["script"]), [str(a) for a in args.get("args", [])],
                                 stdin=args.get("stdin"))
        else:
            raise ToolError("provide `command` (verbatim from the skill) or `script` + `args`")
        _mirror_memlog(rt, ctx, res.command)
        rt.artifacts.scan(ctx.session.session_id, (ctx.session.active_skill or {}).get("skill_id"),
                          since=None)
        return res.for_llm()

    def run_command(args: dict[str, Any], ctx: ToolContext) -> str:
        cmd = args.get("command") or ""
        if args.get("background"):
            bp = rt.commands.start_background(cmd, args.get("cwd"))
            return (f"started {bp.proc_id} (pid {bp.pid}) in {bp.cwd}: $ {' '.join(bp.command)}\n"
                    f"log: {bp.log}\nCheck it with process_status, stop it with stop_process when done.")
        res = rt.commands.run(cmd, args.get("cwd"), timeout_s=args.get("timeout_s"))
        rt.workflow.on_decision(ctx.session, "command", f"{' '.join(res.command)} → "
                                f"{'ok' if res.ok else 'FAILED'} ({res.returncode})")
        return res.for_llm()

    def process_status(args: dict[str, Any], ctx: ToolContext) -> str:
        return rt.commands.status(args["id"])

    def stop_process(args: dict[str, Any], ctx: ToolContext) -> str:
        return rt.commands.stop(args["id"])

    # ------------------------------------------------------------- BMAD control
    def invoke_skill(args: dict[str, Any], ctx: ToolContext) -> str:
        skill = args["skill"]
        mode = args.get("mode")
        top = ctx.session.active_skill
        if mode is None:
            nested = bool(top and top.get("kind") != "agent")
            target = skill.removeprefix("skill:").split()[0]
            mode = "isolated" if nested and target in ISOLATE_WHEN_NESTED else "inline"
        if mode == "isolated":
            rec = rt.skills.run_isolated(ctx, skill, args.get("input") or "", persona=args.get("persona"),
                                         output_file=args.get("output_file"))
            return json.dumps(rec, ensure_ascii=False)
        return rt.skills.enter(ctx.session, ctx, skill, user_input=args.get("input"),
                               forwarded=args.get("forwarded"))

    def complete_workflow(args: dict[str, Any], ctx: ToolContext) -> str:
        result = args.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = {"text": result}
        return rt.skills.complete(ctx.session, ctx, str(args.get("status") or "complete").lower(),
                                  args.get("summary") or "", list(args.get("artifacts") or []), result)

    def ask_user(args: dict[str, Any], ctx: ToolContext) -> str:
        question = args.get("question") or ""
        options = list(args.get("options") or [])
        if ctx.headless:
            if rt.answer_provider is not None:
                answer = rt.answer_provider({"question": question, "options": options,
                                             "context": args.get("context"), "session_id": ctx.session.session_id,
                                             "skill": (ctx.session.active_skill or {}).get("skill_id"),
                                             "persona": (ctx.session.persona or {}).get("agent_id")})
                rt.workflow.on_decision(ctx.session, "answer", f"{question} → {answer}")
                return f"Answer from the Planner Agent (acting for the user): {answer}"
            rt.workflow.on_decision(ctx.session, "assumption", f"unanswered (headless): {question}")
            return ("HEADLESS: no human is available. Decide using the inputs and BMAD guidance, record the choice "
                    "as an assumption (memlog `--type assumption` / [ASSUMPTION] tag / open_questions), and continue.")
        rt.workflow.on_ask_user(ctx.session, question, options, args.get("context"))
        ctx.request_yield("ask_user")
        return "Question shown to the user. Stop here; their reply arrives as the next user message."

    def activate_agent(args: dict[str, Any], ctx: ToolContext) -> str:
        if ctx.session.metadata.get("plan_step_skill"):
            raise ToolError("This is an orchestrated step: the orchestrator assigns agents per step. Finish the "
                            "current skill with complete_workflow and the next agent takes over automatically.")
        persona = rt.agents.activate(ctx.session, ctx, args["agent"])
        return (f"Persona {persona['icon']} {persona['name']} ({persona['title']}) is active. Its agent program "
                "is now in your system prompt: run its remaining activation steps (greeting, menu) now.")

    def dismiss_agent(args: dict[str, Any], ctx: ToolContext) -> str:
        rt.agents.dismiss(ctx.session)
        return "Persona dismissed."

    def spawn_subagent(args: dict[str, Any], ctx: ToolContext) -> str:
        rec = rt.subagents.spawn(args["brief"], ctx, persona=args.get("persona"), skill=args.get("skill"),
                                 profile=args.get("profile") or "research", output_file=args.get("output_file"),
                                 label=args.get("label"))
        return json.dumps(rec, ensure_ascii=False)

    def spawn_subagents(args: dict[str, Any], ctx: ToolContext) -> str:
        tasks = args.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            raise ToolError("tasks must be a non-empty list")
        return json.dumps(rt.subagents.spawn_many(tasks[:12], ctx), ensure_ascii=False)

    def list_bmad_skills(args: dict[str, Any], ctx: ToolContext) -> str:
        kind = args.get("kind")
        items = [s.brief() for s in rt.registry.list_skills(kind=kind, include_shims=bool(args.get("include_shims")))]
        agents = [a.brief() for a in rt.registry.list_agents()]
        return json.dumps({"agents": agents, "skills": items}, ensure_ascii=False)

    sub_task = _obj({"brief": S, "label": S, "persona": S, "skill": S,
                     "profile": {"type": "string", "enum": ["readonly", "research", "write", "verify"]}, "output_file": S},
                    ["brief"])
    for tool in [
        Tool("read_file", "Read a file (step files, references, templates, project docs). Relative paths resolve "
             "from the active skill root. Read one step file at a time.",
             _obj({"path": S, "offset": {"type": "integer"}, "max_lines": {"type": "integer"}}, ["path"]),
             read_file, group="read"),
        Tool("list_dir", "List a directory (optionally recursive / glob pattern).",
             _obj({"path": S, "recursive": {"type": "boolean"}, "pattern": S}), list_dir, group="read"),
        Tool("search_files", "Find files by glob and/or containing text under a root.",
             _obj({"root": S, "glob": S, "contains": S}), search_files, group="read"),
        Tool("write_file", "Create or overwrite a text file inside the project (documents, frontmatter updates).",
             _obj({"path": S, "content": S, "mode": {"type": "string", "enum": ["overwrite", "create"]}},
                  ["path", "content"]), write_file, group="write"),
        Tool("append_file", "Append text to a file inside the project (append-only document building).",
             _obj({"path": S, "content": S}, ["path", "content"]), append_file, group="write"),
        Tool("edit_file", "Replace exact text in a project file (e.g. update stepsCompleted / status frontmatter).",
             _obj({"path": S, "old_text": S, "new_text": S, "replace_all": {"type": "boolean"}},
                  ["path", "old_text", "new_text"]), edit_file, group="write"),
        Tool("run_script", "Run a BMAD Python script. Pass the skill's command verbatim in `command` "
             "(e.g. `uv run /abs/_bmad/scripts/memlog.py append --workspace … --type decision --text \"…\"`) "
             "or `script` + `args`.",
             _obj({"command": S, "script": S, "args": {"type": "array", "items": S}, "stdin": S}), run_script,
             group="script"),
        Tool("run_command", "Run ONE developer command in the project (npm install, npm run build, npm test, "
             "node --check x.js, npx …, git status/diff/add/commit, pytest, tsc …). No &&, pipes or redirects. "
             "cwd defaults to the project root (use a subfolder like client/ or server/). Use background=true "
             "for dev servers, then process_status / stop_process.",
             _obj({"command": S, "cwd": S, "timeout_s": {"type": "number"}, "background": {"type": "boolean"}},
                  ["command"]), run_command, group="command"),
        Tool("process_status", "Show status and recent log of a background process started by run_command.",
             _obj({"id": S}, ["id"]), process_status, group="command"),
        Tool("stop_process", "Stop a background process started by run_command.", _obj({"id": S}, ["id"]),
             stop_process, group="command"),
        Tool("invoke_skill", "Invoke a BMAD skill by id (or `skill:<id> args`). mode=inline (default) makes it the "
             "active program in this conversation; mode=isolated runs it in a subagent and returns its result.",
             _obj({"skill": S, "input": S, "mode": {"type": "string", "enum": ["inline", "isolated"]},
                   "forwarded": {"type": "object"}, "persona": S, "output_file": S}, ["skill"]),
             invoke_skill, group="control"),
        Tool("complete_workflow", "Mark the active skill finished. status: complete | partial | blocked. "
             "Include artifact paths and the skill's status JSON (headless) in `result`.",
             _obj({"status": {"type": "string", "enum": ["complete", "partial", "blocked"]}, "summary": S,
                   "artifacts": {"type": "array", "items": S}, "result": {"type": "object"}}, ["status", "summary"]),
             complete_workflow, group="control"),
        Tool("ask_user", "Ask the user one question at a BMAD checkpoint/menu and stop until they answer.",
             _obj({"question": S, "options": {"type": "array", "items": S}, "context": S}, ["question"]),
             ask_user, group="control"),
        Tool("activate_agent", "Activate a BMAD agent persona (e.g. bmad-agent-pm / John, architect / Winston).",
             _obj({"agent": S}, ["agent"]), activate_agent, group="control"),
        Tool("dismiss_agent", "Dismiss the active persona.", _obj({}), dismiss_agent, group="control"),
        Tool("spawn_subagent", "Spawn one isolated subagent (fresh context) for research, extraction or review; "
             "returns its compact result.", sub_task, spawn_subagent, group="control"),
        Tool("spawn_subagents", "Spawn several subagents in parallel (e.g. reviewer gate, research fan-out).",
             _obj({"tasks": {"type": "array", "items": sub_task}}, ["tasks"]), spawn_subagents, group="control"),
        Tool("list_bmad_skills", "List installed BMAD agents and skills.",
             _obj({"kind": {"type": "string", "enum": ["agent", "workflow", "utility", "shim"]},
                   "include_shims": {"type": "boolean"}}), list_bmad_skills, group="read"),
    ]:
        tm.register(tool)


def _track(ctx: ToolContext, p: Path) -> None:
    if str(p) not in ctx.session.artifacts:
        ctx.session.artifacts.append(str(p))


def _mirror_memlog(rt: "BMadRuntime", ctx: ToolContext, command: list[str]) -> None:
    """Mirror memlog.py appends into the workflow engine's decision list."""
    if not any(Path(c).name == "memlog.py" for c in command) or "append" not in command:
        return
    kind, text = "note", ""
    for i, tok in enumerate(command):
        if tok == "--type" and i + 1 < len(command):
            kind = command[i + 1]
        if tok == "--text" and i + 1 < len(command):
            text = command[i + 1]
    if text:
        rt.workflow.on_decision(ctx.session, kind, text)

