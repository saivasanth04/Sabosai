# BMAD Adapter for the NTG Planner Agent

Runs the **BMad Method v6.12.4** (the clone in `Plugins/BMAD-METHOD-6.12.4`) *outside* an AI IDE,
as a Python library your Planner Agent calls. BMAD's skills are Markdown programs written for an IDE
agent that has a shell, file tools, subagents and a human in the loop. `bmad_adapter` supplies those
things itself, so the skills run **unmodified**: personas, step files, checkpoints, memlogs, reviewer
gates, headless JSON status, and everything else.

```
Planner Agent ──► BMadRuntime ──► GoModel /v1/chat/completions   (LLM)
                       │      └─► GoModel /mcp                   (duckduckgo, crawl4ai, context7, github…)
                       └─► BMAD clone (read-only)  +  {project}/_bmad, _bmad-output (read/write)
```

## Components (one module each, in `bmad_adapter/`)

| Component | Module | What it does |
|---|---|---|
| Registry | `registry.py` | Scans `src/**/SKILL.md`, `module.yaml` rosters, `module-help.csv` catalog and `marketplace.json`. It classifies each skill as an agent, workflow, shim or utility. It finds menu codes, headless support, render-based skills and shim targets. Finds: 5 agents, 22 workflows, 21 shims, 2 utilities. |
| Loader | `loader.py` | Loads SKILL.md, binds `{project-root}`/`{skill-root}`/`{skill-name}`, builds a resource *index* (just-in-time: step files are read on demand, never preloaded). For `bmad-build`-style skills it runs BMAD's `render_skill.py` and loads the rendered `workflow.md` snapshot. |
| Agent executor | `agent_executor.py` | Activates personas (John, Winston, Mary, Sally, Amelia) by resolving the `agent` customization block, loading `persistent_facts` (literal and `file:` globs) and the menu. It then hands the agent program to the LLM for greeting and menu dispatch. The persona carries through every skill until it is dismissed. |
| Skill executor | `skill_executor.py` | Runs skills **inline** (pushed on a skill stack; `complete_workflow` pops back to the caller) or **isolated** (in a subagent). Parses `skill:<id> args` directives. Tail-calls v6 shims into their target with forwarded activation. |
| Instruction engine | `instruction_engine.py` | The agent loop: context → GoModel → tool calls → repeat, until the model ends its turn, asks the user, or completes. The same engine runs the main conversation and subagents. |
| Workflow engine | `workflow_engine.py` | Holds the deterministic state around the prose: current step file, steps visited (and a warning on skipped steps), pending question, decisions (memlog entries are mirrored here), completion. It parses headless status JSON, nudges stalled headless runs, and injects a state block into each prompt so runs survive compaction and resume. |
| Configuration resolver | `config_resolver.py` | Replaces the npm installer. Lays down `{project}/_bmad/scripts`, `config.toml`, `config.user.toml`, `_config/bmad-help.csv` and `custom/`. Resolves config by running BMAD's own `resolve_config.py` / `resolve_customization.py`, with an in-process `config_utils` fallback. Never overwrites a real installer's files or your `_bmad/custom` overrides. |
| Script executor | `script_executor.py` | Runs BMAD's Python scripts (`memlog.py`, `sprint_plan.py`, `brain.py`, …) from the exact command line the skill prints, using `uv run` when available and the current Python otherwise. No `shell=True`. Uses a Windows-safe tokenizer and UTF-8 output. |
| Command executor | `command_executor.py` | A scoped developer shell for the `run_command` tool: npm, node, git, pytest, tsc and similar, one command per call, run inside the project. Refuses chaining (`&&`, pipes, redirects), `git push`, `npm publish` and global installs. Dev servers only run as tracked background processes. Also provides the start-up smoke check. |
| Tool manager | `tool_manager.py`, `builtin_tools.py`, `mcp_client.py` | One registry for the built-in tools and the GoModel MCP tools. Built-ins: `read_file`, `write_file`, `edit_file`, `run_script`, `invoke_skill`, `spawn_subagent(s)`, `ask_user`, `complete_workflow`, `activate_agent`. MCP tools are discovered over streamable HTTP (session id, SSE, pagination) and namespaced the way GoModel does it (`duckduckgo_search`). |
| Subagent manager | `subagent_manager.py` | Isolated subagents with fresh context and tool profiles: `readonly`, `research` (adds MCP), `write` (for reviewers writing `review-*.md`). Optional persona or skill as the subagent's program. Runs them in parallel on a thread pool with a depth limit, and saves transcripts to `_bmad/runtime/subagents/`. |
| Context manager | `context_manager.py` | Keeps context within budget. First it clips stale tool output, then it has the LLM summarise older turns into session memory (decisions, answers, paths, current step), and as a last resort it trims. It never splits a tool-call/result pair. |
| Session manager | `session_manager.py` | Atomic JSON per session in `{project}/_bmad/runtime/sessions/`. Supports `pause`, `resume` (also from a new process), `list` and `latest`. |
| Permission manager | `permissions.py` | Reads are limited to the BMAD clone and the project. Writes are limited to the project, and never touch the BMAD clone, `_bmad/scripts` or `.git`. Only BMAD scripts may run. Tool allow/deny/ask patterns apply. Modes: `auto` / `ask` (approval callback) / `readonly`. Every decision is audit-logged. |
| Artifact manager | `artifact_manager.py` | Tracks every document: written via tools, or created by BMAD scripts (rescans the output folders). Classifies it (prd, architecture, epics, story, memlog, review, sprint-status…), reads frontmatter `status`, and keeps an index in `_bmad/runtime/artifacts.json`. |
| Facade | `runtime.py` | `BMadRuntime`: `start`, `send`, `resume`, `pause`, `run_skill`, `status`, `planner_tools()`. |

## How BMAD's IDE assumptions are mapped

| BMAD skill text | Adapter |
|---|---|
| `Run: uv run {project-root}/_bmad/scripts/x.py …` | `run_script` runs the same command against the bootstrapped `_bmad/scripts`. |
| "Read fully and follow `step-02-….md`" | `read_file`. The workflow engine tracks the step. |
| "invoke the `bmad-x` skill", menu `skill = …` | `invoke_skill`, which pushes onto the skill stack. |
| "spawn research / reviewer subagents" | `spawn_subagent(s)`, running in parallel with isolated context. |
| "HALT and wait for input" | Interactive: `ask_user`, the session becomes `waiting_input`. Headless: routed to your planner's `answer_provider`, or recorded as an assumption. |
| "web research" | The GoModel MCP tools from your `config.yaml`. |
| headless JSON status | Parsed into `TurnResult.completion["result"]`. |

> Your GoModel `filesystem` MCP server only allows read tools. That's intentional. BMAD writes its
> PRDs and memlogs through the adapter's own permission-checked `write_file`/`edit_file`, which are
> limited to the project folder.

## Just give it a request (auto-routing)

```bat
python -m bmad_adapter --project C:\Projects\NTG\Agents\Planner_Agent\Workspace\chess-app do "I want to build a chess web application using React & Node.js"
```

The **Orchestrator** (`orchestrator.py`) reads the request, the bmad-help catalog, which agent owns each skill, and what already exists in the project. From that it picks the smallest path that does the job:

| Track | When | Steps (agent) |
|---|---|---|
| quick | small change or bug on existing code | build (Amelia) |
| spec | one contained feature | spec (John) → build (Amelia) |
| method | new product or large feature | PRD (John) → UX (Sally, if there's a UI) → architecture (Winston) → epics & stories (John) → sprint planning (John), plus build with `--through ship` |
| research | market, domain or tech question | deep-recon (Mary) |
| ideation | vague idea | brainstorming / forge-idea (Mary) |
| review | code or document | code-review / review |

Each step runs as its owning agent in a fresh session, and gets the earlier steps' artifacts as input. Interactive mode moves to the next step automatically when one finishes. Inside a step you can type `/plan` to see progress or `/next` to skip ahead. Options: `--auto` runs the whole chain headless, `--plan-only` only shows the route, `--through ship` adds implementation. From Python, use `rt.handle(request, auto=True)`. From your planner's LLM, use the `bmad_handle_request` tool. Plans are saved in `_bmad/runtime/plans/`.

**Guard rails:**
- **Step completion.** A step finishes only when *its own* skill completes; a sub-skill such as review or elicitation doesn't count.
- **No wandering.** Inside a step the agent can't start a different BMAD phase or switch persona. It's told to finish, and the orchestrator hands off to the next step.
- **Reviews in subagents.** A nested `bmad-review` runs as an isolated subagent.
- **One session per story.** Build steps run once per open story in `sprint-status.yaml`, each in a fresh session.
- **Verified builds.** After each story, the orchestrator itself runs `npm install`, build, tests and a server start-up check for every package. Failures go back to the dev agent with the real error output, up to 2 fix rounds.

Run the same checks any time with `python -m bmad_adapter --project <path> verify`.

## Quick start (Windows)

```bat
cd C:\Projects\NTG\Agents\Planner_Agent\Runtime_Manager
set GOMODEL_API_KEY=sk-...           &REM your GoModel key
set GOMODEL_MODEL=<model alias>      &REM or set [gomodel].model in bmad_adapter.toml

python -m bmad_adapter health                                 # registry + MCP status
python -m bmad_adapter --project C:\Projects\NTG\Workspace\app chat --agent pm
python -m bmad_adapter --project C:\Projects\NTG\Workspace\app run bmad-prd --input brief.md --json
python -m bmad_adapter sessions
python -m bmad_adapter resume <session-id>
```

Requirements: Python ≥ 3.11 (you have 3.13). No pip packages are required; PyYAML is used if present.
`uv` is optional: BMAD scripts fall back to the current interpreter.

## Using it from the Planner Agent

```python
from bmad_adapter import BMadRuntime

rt = BMadRuntime.for_project(r"C:\Projects\NTG\Workspace\habit-tracker",
                             answer_provider=lambda q: my_planner.decide(q))   # headless checkpoints

# Headless, one call per skill (returns when the skill is done)
r = rt.run_skill("bmad-prd", {"intent": "create", "brief": "..."})
r.status                    # completed | blocked | failed
r.completion["result"]      # BMAD's headless JSON: prd path, assumptions, open_questions…
r.artifacts                 # documents created/changed

# Interactive, persona-driven (pass messages from a human or from the planner)
r = rt.start(agent="pm", message="Let's write the PRD")
r = rt.send(r.session_id, "Fast path")
rt.pause(r.session_id); rt.resume(r.session_id)

# Or give the planner's own LLM BMAD as tools
schemas, dispatch = rt.planner_tools()    # bmad_catalog, bmad_run_skill, bmad_send, bmad_status, …
```

See `examples/planner_integration.py` for the full analysis → PRD → architecture → epics → sprint chain.

## Customizing BMAD (unchanged BMAD mechanism)

Put overrides in `{project}/_bmad/custom/<skill>.toml` (team) or `<skill>.user.toml` (personal), e.g.
`bmad-agent-pm.toml` to add persistent facts or menu items, and `bmad-prd.toml` to change templates,
reviewers or `external_handoffs`. Central overrides go in `_bmad/custom/config.toml`. The adapter
honors BMAD's merge rules: scalars override, tables deep-merge, and `code`/`id` arrays replace in
place.

## Tests

```bat
python -m pytest tests -q
```

There are 30 end-to-end tests, run against the real v6.12.4 skill files with a scripted LLM and a fake
GoModel chat and MCP server. They cover: install and config resolution through BMAD's own scripts,
custom override layers, the full interactive John → PRD → memlog → back-to-menu flow, headless status
JSON, nudges, the planner answer provider, parallel reviewer subagents, read-only research subagents,
render-based `bmad-build` with step tracking and the skip warning, shim tail-calls, permissions, ask
mode, pause/resume across processes, context compaction, MCP over SSE with session ids, and the
planner tools.

## Notes

* The old `__pycache__/bmad_loader*.pyc` and `bmad_registry*.pyc` came from an earlier attempt whose
  `.py` sources no longer exist. `bmad_adapter` replaces them, and they can be deleted.
* Changing the model is a config change. BMAD skills are long and tool-heavy, so use a strong
  tool-calling model for the main loop and a cheaper `subagent_model` for research and reviews.
