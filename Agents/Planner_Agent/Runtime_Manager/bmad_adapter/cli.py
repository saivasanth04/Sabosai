"""Command line for the BMAD adapter.

    python -m bmad_adapter do "I want to build a chess web app using React & Node.js"   # auto-routing
    python -m bmad_adapter do "…" --auto            # run the whole chain headless
    python -m bmad_adapter do "…" --plan-only       # just show which agents/skills it would use
    python -m bmad_adapter health
    python -m bmad_adapter list [--kind workflow] [--shims]
    python -m bmad_adapter install --project C:\\Projects\\NTG\\Workspace\\my-app
    python -m bmad_adapter chat --agent pm [--project …]
    python -m bmad_adapter chat --skill bmad-brainstorming
    python -m bmad_adapter run bmad-prd --input brief.md [--json]
    python -m bmad_adapter sessions
    python -m bmad_adapter resume <session-id>
    python -m bmad_adapter artifacts [--kind prd]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .permissions import PermissionRequest
from .runtime import BMadRuntime, TurnResult
from .settings import AdapterSettings


def _console_approval(req: PermissionRequest) -> bool:
    detail = json.dumps(req.detail, ensure_ascii=False)[:300]
    ans = input(f"\n[permission] allow {req.category} → {req.target} {detail}? [y/N] ").strip().lower()
    return ans in {"y", "yes"}


def _runtime(args: argparse.Namespace, *, connect_mcp: bool = True) -> BMadRuntime:
    overrides = {}
    if args.project:
        overrides["project_root"] = args.project
    if args.bmad_root:
        overrides["bmad_root"] = args.bmad_root
    if getattr(args, "model", None):
        overrides["gomodel"] = {"model": args.model}
    settings = AdapterSettings.load(args.config, **overrides)
    return BMadRuntime(settings, approval_callback=_console_approval, connect_mcp=connect_mcp and not args.no_mcp)


def _print_turn(r: TurnResult) -> None:
    if r.message:
        print("\n" + r.message.strip() + "\n")
    meta = [f"status={r.status}"]
    if r.skill:
        meta.append(f"skill={r.skill}")
    if r.step:
        meta.append(f"step={Path(r.step).name}")
    if r.artifacts:
        meta.append("artifacts=" + ", ".join(a["path"] for a in r.artifacts))
    if r.error:
        meta.append(f"note={r.error}")
    print("  [" + " | ".join(meta) + "]")
    if r.pending_question and r.pending_question.get("options"):
        for i, opt in enumerate(r.pending_question["options"], 1):
            print(f"   {i}. {opt}")


def _interactive_plan(rt: BMadRuntime, h) -> None:
    orch = rt.orchestrator
    print(f"▶ {h.note}")
    turn = h.turn
    while turn is not None:
        _print_turn(turn)
        if h.finished:
            print("\n" + h.plan.describe() + "\n" + h.note)
            return
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = "/pause"
        if not line:
            continue
        if line in {"/quit", "/exit", "/pause"}:
            rt.pause(turn.session_id)
            print(f"paused. plan {h.plan.plan_id}; resume this step with: "
                  f"python -m bmad_adapter --project {rt.project_root} resume {turn.session_id}")
            return
        if line == "/plan":
            print(h.plan.describe())
            continue
        if line == "/next":                      # skip ahead manually
            st = h.plan.current
            if st:
                st.status = "skipped"
            h = orch._start_step(h.plan)
            print(f"\n▶ {h.note}")
            turn = h.turn
            continue
        turn = rt.send(turn.session_id, line)
        h = orch.advance(h.plan, turn)
        if h.turn is not None and h.turn.session_id != turn.session_id:
            _print_turn(turn)
            print(f"\n✔ step done → ▶ {h.note}\n")
        elif h.note and "verification" in h.note:
            _print_turn(turn)
            print(f"\n⚠ {h.note}\n")
        turn = h.turn


def _events(event: dict) -> None:
    if event.get("type") == "tool_call":
        pad = "  " * (event.get("depth", 0) + 1)
        args = json.dumps(event.get("arguments"), ensure_ascii=False)[:140]
        print(f"{pad}↳ {event['name']} {args}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bmad_adapter", description="Run BMAD Method skills outside an IDE.")
    p.add_argument("--config", help="path to bmad_adapter.toml")
    p.add_argument("--project", help="target project root")
    p.add_argument("--bmad-root", help="BMAD-METHOD clone")
    p.add_argument("--no-mcp", action="store_true", help="do not connect to GoModel MCP")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    do = sub.add_parser("do", help="plain request → BMAD picks agents/skills and runs them in order")
    do.add_argument("request")
    do.add_argument("--auto", action="store_true", help="run every step headless, no questions")
    do.add_argument("--plan-only", action="store_true")
    do.add_argument("--through", choices=["plan", "ship"], default="plan",
                    help="plan = stop after sprint planning (default); ship = also implement")
    do.add_argument("--model")
    sub.add_parser("verify", help="install/build/test/start-up check of the project's code (no LLM)")
    sub.add_parser("health")
    ls = sub.add_parser("list")
    ls.add_argument("--kind")
    ls.add_argument("--shims", action="store_true")
    sub.add_parser("install")
    ch = sub.add_parser("chat")
    ch.add_argument("--agent")
    ch.add_argument("--skill")
    ch.add_argument("--model")
    ch.add_argument("message", nargs="?")
    rn = sub.add_parser("run")
    rn.add_argument("skill")
    rn.add_argument("--input", help="text, or a path to a file with the brief/payload")
    rn.add_argument("--agent")
    rn.add_argument("--model")
    rn.add_argument("--json", action="store_true")
    sub.add_parser("sessions")
    rs = sub.add_parser("resume")
    rs.add_argument("session_id")
    ar = sub.add_parser("artifacts")
    ar.add_argument("--kind")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    if args.cmd in {"list", "install", "sessions", "artifacts", "verify"}:
        rt = _runtime(args, connect_mcp=False)
    else:
        rt = _runtime(args)
    rt.engine.on_event = _events

    try:
        return _dispatch(args, rt)
    finally:
        rt.commands.stop_all()


def _print_verification(v: dict) -> None:
    for c in v["checks"]:
        mark = {True: "✔", False: "✖", None: "–"}[c["ok"]]
        print(f" {mark} {c['check']:<22} {c.get('cmd', ''):<28} {c['cwd']}")
        if c["ok"] is False:
            print("    " + c.get("tail", "")[-1500:].replace("\n", "\n    "))
    print("\nVERIFY:", "PASSED" if v["ok"] else "FAILED", v.get("note", ""))


def _dispatch(args: argparse.Namespace, rt: BMadRuntime) -> int:
    if args.cmd == "verify":
        v = rt.orchestrator.verify()
        _print_verification(v)
        return 0 if v["ok"] else 1
    if args.cmd == "health":
        print(json.dumps(rt.health(), indent=2, ensure_ascii=False))
    elif args.cmd == "list":
        print(json.dumps({"agents": rt.list_agents(), "skills": rt.list_skills(args.kind, args.shims)},
                         indent=2, ensure_ascii=False))
    elif args.cmd == "install":
        print(json.dumps(rt.config.ensure_installed(force=False), indent=2))
    elif args.cmd == "sessions":
        print(json.dumps(rt.list_sessions(), indent=2, default=str))
    elif args.cmd == "artifacts":
        print(json.dumps(rt.artifacts.as_dicts(kind=args.kind), indent=2, default=str))
    elif args.cmd == "run":
        payload = args.input
        if payload and Path(payload).is_file():
            payload = Path(payload).read_text(encoding="utf-8")
        r = rt.run_skill(args.skill, payload, agent=args.agent, headless=True)
        print(json.dumps(r.to_dict(), indent=2, default=str, ensure_ascii=False)) if args.json else _print_turn(r)
        return 0 if r.status in {"completed"} else 1
    elif args.cmd == "do" or (args.cmd == "chat" and args.message and not args.agent and not args.skill):
        request = args.request if args.cmd == "do" else args.message
        auto = getattr(args, "auto", False)
        through = getattr(args, "through", "plan")
        orch = rt.orchestrator
        print("Routing your request through BMAD...", flush=True)
        plan = orch.route(request, through)
        print("\n" + plan.describe() + f"\n  (router: {plan.router}, plan id {plan.plan_id})\n")
        if getattr(args, "plan_only", False):
            return 0
        h = orch.start(request, auto=auto, plan=plan)
        if auto:
            print(h.plan.describe())
            for s_ in h.plan.steps:
                if s_.verification and not s_.verification["ok"]:
                    print(f"\nChecks for {s_.skill} [{s_.story or ''}]:")
                    _print_verification(s_.verification)
            print("\n" + h.note)
            for s_ in h.plan.steps:
                for a in s_.artifacts:
                    print(f"   {s_.skill}: {a}")
            return 0 if all(s_.status == "completed" for s_ in h.plan.steps) else 1
        _interactive_plan(rt, h)
        return 0
    elif args.cmd in {"chat", "resume"}:
        if args.cmd == "resume":
            r = rt.resume(args.session_id)
        else:
            r = rt.start(agent=args.agent, skill=args.skill, message=args.message, headless=False)
        _print_turn(r)
        print(f"  (session {r.session_id} — type /pause, /status, /quit)")
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                line = "/pause"
            if not line:
                continue
            if line in {"/quit", "/exit"}:
                break
            if line == "/pause":
                rt.pause(r.session_id)
                print(f"paused. resume with: python -m bmad_adapter resume {r.session_id}")
                break
            if line == "/status":
                print(json.dumps(rt.status(r.session_id).to_dict(), indent=2, default=str, ensure_ascii=False))
                continue
            r = rt.send(r.session_id, line)
            _print_turn(r)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
