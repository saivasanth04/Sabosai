"""Built-in runtime tools exposed to the LLM (the "IDE" surface BMAD skills expect)."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import BMadLoaderError, ToolError
from .loader import TEXT_SUFFIXES
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
    # All agent file I/O goes through rt.fs → GoModel filesystem MCP (or local fallback). The adapter keeps
    # its own guarantees around it: permission checks, BMAD token binding, step tracking, artifact tracking.
    fs = rt.fs

    def _skill_id(ctx: ToolContext) -> str | None:
        return (ctx.session.active_skill or {}).get("skill_id")

    def _record(ctx: ToolContext, p: Path, action: str):
        art = rt.artifacts.record(p, action=action, session_id=ctx.session.session_id, skill_id=_skill_id(ctx))
        _track(ctx, p)
        return art

    def read_file(args: dict[str, Any], ctx: ToolContext) -> str:
        path = args.get("path") or ""
        head, tail = args.get("head"), args.get("tail")
        if args.get("max_lines") and head is None:
            head = int(args.get("offset") or 0) + int(args["max_lines"])
        via = {"backend": "local"}

        def reader(p: Path) -> str:
            text, via["backend"] = fs.run("read", "read", p, head=head, tail=tail)
            return text
        try:
            res = rt.loader.read_resource(path, ctx.skill, reader=reader)
            p, content = res.path, res.content
        except BMadLoaderError:
            p = rt.permissions.check_read(path)
            if not p.is_file():
                raise ToolError(f"file not found: {p}")
            content = reader(p).replace("{project-root}", str(rt.project_root))
        else:
            rt.permissions.check_read(p)
        if args.get("offset"):
            content = "\n".join(content.splitlines()[int(args["offset"]):])
        warning = rt.workflow.on_file_read(ctx.session, p)
        return f"# {p}  [{via['backend']}]\n" + content + (f"\n\n{warning}" if warning else "")

    def read_multiple_files(args: dict[str, Any], ctx: ToolContext) -> str:
        paths = [rt.permissions.check_read(x) for x in (args.get("paths") or [])][:20]
        if not paths:
            raise ToolError("paths must be a non-empty list")
        if fs.backend_for("read_many", *paths) is fs.mcp and fs.mcp is not None and fs.mcp.has("read_many"):
            fs.stats.mcp_calls += 1
            text = fs.mcp.read_many(paths)
        else:
            text = "\n---\n".join(f"{p}:\n{fs.run('read', 'read', p)[0]}" for p in paths)
        for p in paths:
            rt.workflow.on_file_read(ctx.session, p)
        return text.replace("{project-root}", str(rt.project_root))

    def list_dir(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_read(args.get("path") or str(rt.project_root))
        if args.get("recursive"):
            return directory_tree({"path": str(p), "depth": args.get("depth", 4)}, ctx)
        entries, via = fs.run("list", "list", p)
        pattern = args.get("pattern")
        rows = []
        for e in entries:
            if e.name in {".git", "node_modules", "__pycache__"} or (pattern and not fnmatch.fnmatch(e.name, pattern)):
                continue
            rows.append(("dir " if e.is_dir else f"{e.size if e.size is not None else '?':>8} B") + f"  {e.name}")
        return f"# {p}  [{via}]\n" + ("\n".join(rows) or "(empty)")

    def directory_tree(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_read(args.get("path") or str(rt.project_root))
        exclude = list(args.get("exclude") or []) + ["node_modules", ".git", "__pycache__", "render"]
        depth = args.get("depth", 4)
        tree, via = fs.run("tree", "tree", p, depth=int(depth) if depth else None, exclude=exclude)
        lines: list[str] = []

        def walk(nodes, indent):
            for n in nodes:
                lines.append("  " * indent + (n["name"] + "/" if n.get("type") == "directory" else n["name"]))
                if len(lines) > 600:
                    return
                walk(n.get("children") or [], indent + 1)
        walk(tree, 0)
        return f"# {p}  [{via}]\n" + "\n".join(lines[:600])

    def file_info(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_read(args["path"])
        text, via = fs.run("info", "info", p)
        return f"# {p}  [{via}]\n{text}"

    def search_files(args: dict[str, Any], ctx: ToolContext) -> str:
        root = rt.permissions.check_read(args.get("root") or str(rt.project_root))
        glob_pat = args.get("glob") or "**/*"
        exclude = list(args.get("exclude") or []) + ["node_modules", ".git", "__pycache__"]
        paths, via = fs.run("search", "search", root, pattern=glob_pat, exclude=exclude)
        needle = (args.get("contains") or "").lower()
        if not needle:
            return f"[{via}]\n" + ("\n".join(paths[:300]) or "no matches")
        hits = []                                # content search: read candidate files through the backend
        for path in paths[:400]:
            p = Path(path)
            if p.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = fs.run("read", "read", p)[0]
            except Exception:  # noqa: BLE001
                continue
            idx = text.lower().find(needle)
            if idx != -1:
                line_no = text.count("\n", 0, idx) + 1
                hits.append(f"{p}:{line_no}: {text[max(0, idx - 60): idx + 100].replace(chr(10), ' ')}")
            if len(hits) >= 200:
                break
        return f"[{via}]\n" + ("\n".join(hits) or "no matches")

    def write_file(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_write(args["path"])
        if args.get("mode") == "create" and p.exists():
            raise ToolError(f"{p} already exists (mode=create) — use edit_file or mode=overwrite")
        content = args.get("content", "")
        _, via = fs.run("write", "write", p, content=content)
        art = _record(ctx, p, "write")
        return (f"wrote {p} ({len(content)} chars) [{via}]"
                + (f" [artifact: {art.kind}, status={art.status}]" if art else ""))

    def append_file(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_write(args["path"])
        content = args.get("content", "")
        _, via = fs.run("append", "append", p, content=content)
        _record(ctx, p, "append")
        return f"appended {len(content)} chars to {p} [{via}]"

    def edit_file(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_write(args["path"])
        if not p.is_file():
            raise ToolError(f"file not found: {p}")
        edits = args.get("edits")
        if not edits:
            old, new = args.get("old_text", ""), args.get("new_text", "")
            if not old:
                raise ToolError("give `edits` [{oldText,newText}] or `old_text` + `new_text`")
            if args.get("replace_all"):            # MCP edit replaces first match only → full rewrite
                text = fs.run("read", "read", p)[0]
                if old not in text:
                    raise ToolError("old_text not found — re-read the file and copy the exact text")
                count = text.count(old)
                _, via = fs.run("write", "write", p, content=text.replace(old, new))
                _record(ctx, p, "edit")
                return f"edited {p}: {count} replacement(s) [{via}]"
            edits = [{"oldText": old, "newText": new}]
        edits = [{"oldText": e.get("oldText", e.get("old_text", "")), "newText": e.get("newText", e.get("new_text", ""))}
                 for e in edits]
        diff, via = fs.run("edit", "edit", p, edits=edits, dry_run=bool(args.get("dry_run")))
        if not args.get("dry_run"):
            _record(ctx, p, "edit")
        return f"edited {p} [{via}]\n{diff[:4000]}"

    def create_directory(args: dict[str, Any], ctx: ToolContext) -> str:
        p = rt.permissions.check_write(args["path"])
        _, via = fs.run("mkdir", "mkdir", p)
        return f"created {p} [{via}]"

    def move_file(args: dict[str, Any], ctx: ToolContext) -> str:
        src = rt.permissions.check_write(args["source"])
        dst = rt.permissions.check_write(args["destination"])
        _, via = fs.run("move", "move", src, dst)
        if dst.is_file():
            _record(ctx, dst, "move")
        return f"moved {src} → {dst} [{via}]"

    def copy_file(args: dict[str, Any], ctx: ToolContext) -> str:
        src = rt.permissions.check_read(args["source"])
        dst = rt.permissions.check_write(args["destination"])
        _, via = fs.run("copy", "copy", src, dst)
        _record(ctx, dst, "copy")
        return f"copied {src} → {dst} [{via}]"

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
        Tool("read_file", "Read a text file (step files, references, templates, project docs). Relative paths resolve "
             "from the active skill root. Optional head/tail = first/last N lines. Read one step file at a time.",
             _obj({"path": S, "head": {"type": "integer"}, "tail": {"type": "integer"},
                   "offset": {"type": "integer"}, "max_lines": {"type": "integer"}}, ["path"]), read_file, group="read"),
        Tool("read_multiple_files", "Read several text files at once (e.g. all planning artifacts for context).",
             _obj({"paths": {"type": "array", "items": S}}, ["paths"]), read_multiple_files, group="read"),
        Tool("list_dir", "List a directory ([dir] / size + name). recursive=true shows a tree.",
             _obj({"path": S, "recursive": {"type": "boolean"}, "pattern": S, "depth": {"type": "integer"}}),
             list_dir, group="read"),
        Tool("directory_tree", "Recursive tree of a directory (node_modules/.git excluded). depth limits levels.",
             _obj({"path": S, "depth": {"type": "integer"}, "exclude": {"type": "array", "items": S}}),
             directory_tree, group="read"),
        Tool("file_info", "Size, timestamps and type of a file or directory.", _obj({"path": S}, ["path"]),
             file_info, group="read"),
        Tool("search_files", "Find files by glob under a root ('**/*.jsx', '*.md'); add `contains` to also search "
             "inside them for text.", _obj({"root": S, "glob": S, "contains": S,
                                             "exclude": {"type": "array", "items": S}}), search_files, group="read"),
        Tool("write_file", "Create or overwrite a text file inside the project (parent folders are created).",
             _obj({"path": S, "content": S, "mode": {"type": "string", "enum": ["overwrite", "create"]}},
                  ["path", "content"]), write_file, group="write"),
        Tool("append_file", "Append text to a file inside the project (created if missing; no newline added).",
             _obj({"path": S, "content": S}, ["path", "content"]), append_file, group="write"),
        Tool("edit_file", "Edit a project file: `edits` = [{oldText, newText}, …] applied in order (each replaces the "
             "first match; line endings/indentation tolerant), or `old_text` + `new_text` (+ replace_all). "
             "Returns a diff. dry_run=true previews.",
             _obj({"path": S, "edits": {"type": "array", "items": _obj({"oldText": S, "newText": S},
                                                                       ["oldText", "newText"])},
                   "old_text": S, "new_text": S, "replace_all": {"type": "boolean"}, "dry_run": {"type": "boolean"}},
                  ["path"]), edit_file, group="write"),
        Tool("create_directory", "Create a directory (and parents) inside the project.", _obj({"path": S}, ["path"]),
             create_directory, group="write"),
        Tool("move_file", "Move or rename a file/directory inside the project (fails if destination exists).",
             _obj({"source": S, "destination": S}, ["source", "destination"]), move_file, group="write"),
        Tool("copy_file", "Copy a file to a new path inside the project (never overwrites).",
             _obj({"source": S, "destination": S}, ["source", "destination"]), copy_file, group="write"),
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

