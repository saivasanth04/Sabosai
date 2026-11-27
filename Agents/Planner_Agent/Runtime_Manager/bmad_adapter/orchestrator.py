"""Orchestrator — give a plain request, BMAD picks the agents and skills.

    rt.handle("I want to build a chess web app using React & Node.js")

1. **Route.** The LLM reads the request, the bmad-help catalog (phases, preceded-by /
   followed-by, required gates), the agent → skill ownership from each agent's menu and
   what already exists in the project, and sizes the work the BMAD way:

   =========  ==============================================================
   quick      small change / bug fix on existing code → bmad-build (Amelia)
   spec       clear, contained feature → bmad-spec → bmad-build
   method     new product / big feature → brief? → PRD → UX? → architecture
              → epics & stories → sprint planning (→ build when --through ship)
   research   market / domain / technical question → bmad-deep-recon (Mary)
   ideation   vague idea → bmad-brainstorming or bmad-forge-idea
   review     review code or a document → bmad-code-review / bmad-review
   =========  ==============================================================

   A keyword heuristic takes over if the model's answer is unusable.

2. **Run.** Each step runs as the agent that owns it, in a fresh session (BMAD's own
   advice: one workflow per fresh chat). Artifacts produced so far are handed to the next
   step. Interactive mode advances automatically when a step's skill completes; auto mode
   runs every step headless and stops at the first blocked/failed step.

Plans are saved to ``{project}/_bmad/runtime/plans/<id>.json`` so a chain can be resumed.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .session_manager import COMPLETED

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import BMadRuntime, TurnResult

PHASE_ORDER = ["anytime", "plan", "ship"]
PLANNING_SKILLS = {"bmad-product-brief", "bmad-prfaq", "bmad-prd", "bmad-ux", "bmad-architecture",
                   "bmad-create-epics-and-stories", "bmad-sprint-planning", "bmad-spec"}
SHIP_SKILLS = {"bmad-build", "bmad-build-auto", "bmad-code-review", "bmad-qa-generate-e2e-tests",
               "bmad-retrospective", "bmad-walkthrough"}
CORE_OWNER = {"bmad-deep-recon": "bmad-agent-analyst", "bmad-brainstorming": "bmad-agent-analyst",
              "bmad-forge-idea": "bmad-agent-analyst", "bmad-spec": "bmad-agent-pm",
              "bmad-review": None, "bmad-party-mode": None, "bmad-help": None}

STEP_DIRECTIVES = {
    "bmad-architecture": "Include the project's source-tree layout (top-level folders — e.g. separate frontend and "
                         "backend packages — where tests live, and each package's manifest) so builders place code "
                         "consistently.",
    "bmad-create-epics-and-stories": "Base requirements only on artifacts that exist; do not cite UX/design "
                                     "requirements unless a UX document exists.",
    "bmad-build": "Follow the architecture's source-tree layout. Declare every imported package in the right "
                  "manifest, install dependencies, build, run the tests and a start-up smoke check with run_command, "
                  "and update this story to `done` in sprint-status.yaml. Only then call complete_workflow. The "
                  "orchestrator re-runs install/build/test/start-up checks afterwards and sends failures back.",
}
MAX_FIX_ATTEMPTS = 2

ROUTER_PROMPT = """You are the BMAD Method router inside the NTG Planner Agent.
BMAD is agile AI-driven development: the process sizes itself to the work. Small changes go
straight to build; complex work gets the depth it needs (brief → PRD → UX → architecture →
epics & stories → sprint planning → build). Pick the SMALLEST sufficient path.

Tracks:
- quick: small change, bug fix, tweak on an existing codebase → bmad-build
- spec: one clear, contained feature that needs pinning down first → bmad-spec, bmad-build
- method: a new product/app/system or a large multi-part feature → planning chain
  (include bmad-product-brief only if the idea is still fuzzy; include bmad-ux only if there is a
  meaningful user interface; always architecture before epics; sprint planning last in planning)
- research: market / domain / technical / competitive question → bmad-deep-recon
- ideation: vague idea, wants options → bmad-brainstorming (or bmad-forge-idea to stress-test one idea)
- review: review code or a document → bmad-code-review (code) or bmad-review (documents)

Skip steps whose artifact already exists with status final (see EXISTING ARTIFACTS).
Use only skill ids from the CATALOG and the owning agent from AGENT SKILLS.
Return ONLY JSON:
{"track": "...", "scale": "small|medium|large", "reason": "one sentence",
 "steps": [{"skill": "bmad-…", "agent": "bmad-agent-… or null", "why": "what this step produces"}]}"""


@dataclass
class PlanStep:
    skill: str
    agent: str | None
    why: str
    status: str = "pending"              # pending | running | completed | blocked | failed | skipped
    session_id: str | None = None
    artifacts: list[str] = field(default_factory=list)
    summary: str = ""
    started: float | None = None
    story: str | None = None             # bmad-build steps: one sprint-status story per step
    verification: dict[str, Any] | None = None
    fix_attempts: int = 0
    last_fix: float | None = None


@dataclass
class RoutePlan:
    plan_id: str
    request: str
    track: str
    scale: str
    reason: str
    steps: list[PlanStep]
    through: str = "plan"                # plan | ship
    router: str = "llm"                  # llm | heuristic
    created: float = field(default_factory=time.time)

    @property
    def current(self) -> PlanStep | None:
        return next((s for s in self.steps if s.status in {"pending", "running"}), None)

    def index(self, step: PlanStep) -> int:
        return self.steps.index(step) + 1

    def describe(self) -> str:
        lines = [f"Track: {self.track} ({self.scale}) — {self.reason}"]
        for i, s in enumerate(self.steps, 1):
            mark = {"completed": "✔", "running": "▶", "blocked": "✖", "failed": "✖", "skipped": "–"}.get(s.status, " ")
            label = s.skill + (f" [{s.story}]" if s.story else "")
            ver = ""
            if s.verification:
                ver = "  (checks passed)" if s.verification.get("ok") else "  (CHECKS FAILED)"
            lines.append(f"  {mark} {i}. {label:<32} {s.agent or '(no persona)':<26} {s.why}{ver}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoutePlan":
        d = dict(d)
        d["steps"] = [PlanStep(**s) for s in d.get("steps", [])]
        return cls(**d)


@dataclass
class HandleResult:
    plan: RoutePlan
    turn: "TurnResult | None" = None     # interactive: the live turn of the current step
    finished: bool = False
    note: str = ""


class Orchestrator:
    def __init__(self, runtime: "BMadRuntime") -> None:
        self.rt = runtime
        self.plan_dir = runtime.project_root / "_bmad" / "runtime" / "plans"
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        self._owner = self._skill_owners()

    # ================================================================ routing
    def route(self, request: str, through: str = "plan") -> RoutePlan:
        plan = self._route_llm(request) or self._route_heuristic(request)
        plan.through = through
        if through != "ship" and plan.track == "method":
            plan.steps = [s for s in plan.steps if s.skill not in SHIP_SKILLS] or plan.steps
        if not plan.steps:
            plan.steps = [PlanStep("bmad-help", None, "figure out where to start")]
        self.save(plan)
        return plan

    def _route_llm(self, request: str) -> RoutePlan | None:
        catalog = []
        for s in self.rt.registry.list_skills(include_shims=False):
            if s.kind == "agent":
                continue
            h = s.help
            catalog.append({"skill": s.skill_id, "phase": h.phase if h else s.category,
                            "after": h.preceded_by if h else "", "before": h.followed_by if h else "",
                            "required": h.required if h else False,
                            "what": re.sub(r"\s+", " ", (h.description if h and h.description else s.description))[:220]})
        existing = [{"path": a["path"], "kind": a["kind"], "status": a["status"]}
                    for a in self.rt.artifacts.as_dicts()][:40]
        code_present = self._has_code()
        user = (f"REQUEST:\n{request}\n\nPROJECT: {self.rt.project_root} "
                f"(existing source code: {'yes' if code_present else 'no'})\n\n"
                f"EXISTING ARTIFACTS:\n{json.dumps(existing, ensure_ascii=False)}\n\n"
                f"AGENT SKILLS:\n{json.dumps(self._owner_view(), ensure_ascii=False)}\n\n"
                f"CATALOG:\n{json.dumps(catalog, ensure_ascii=False)}")
        try:
            resp = self.rt.llm.chat([{"role": "system", "content": ROUTER_PROMPT},
                                     {"role": "user", "content": user}], None, temperature=0.1, max_tokens=1200)
            data = _json_from(resp.content or "")
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            return None
        steps: list[PlanStep] = []
        for raw in data["steps"]:
            if not isinstance(raw, dict):
                continue
            info = self.rt.registry.find(str(raw.get("skill", "")))
            if info is None or info.kind in {"agent", "shim"}:
                continue
            if any(s.skill == info.skill_id for s in steps):
                continue
            agent = raw.get("agent") if raw.get("agent") in self._owners_of(info.skill_id) else None
            steps.append(PlanStep(info.skill_id, agent or self._owner.get(info.skill_id), str(raw.get("why", ""))[:200]))
        if not steps:
            return None
        return RoutePlan(_pid(), request, str(data.get("track", "method")), str(data.get("scale", "medium")),
                         str(data.get("reason", ""))[:300], self._order(steps), router="llm")

    def _route_heuristic(self, request: str) -> RoutePlan:
        r = request.lower()
        code = self._has_code()
        s = lambda sk, why: PlanStep(sk, self._owner.get(sk), why)  # noqa: E731
        if re.search(r"\b(research|compare|market|competitor|landscape|which (library|framework|tool))\b", r):
            return RoutePlan(_pid(), request, "research", "small", "research question",
                             [s("bmad-deep-recon", "researched findings with sources")], router="heuristic")
        if re.search(r"\b(brainstorm|ideas?|not sure|thinking about|options for)\b", r) and not re.search(r"\bbuild\b", r):
            return RoutePlan(_pid(), request, "ideation", "small", "idea still open",
                             [s("bmad-brainstorming", "range of options, converged")], router="heuristic")
        if re.search(r"\breview\b", r):
            return RoutePlan(_pid(), request, "review", "small", "review request",
                             [s("bmad-code-review" if code else "bmad-review", "review findings")], router="heuristic")
        if code and re.search(r"\b(fix|bug|typo|rename|tweak|small|update|change|add (a|an) )\b", r):
            return RoutePlan(_pid(), request, "quick", "small", "small change on existing code",
                             [s("bmad-build", "implemented, reviewed change")], router="heuristic")
        ui = bool(re.search(r"\b(web|ui|ux|app|application|frontend|react|vue|angular|mobile|dashboard|site)\b", r))
        steps = [s("bmad-prd", "requirements: features, FRs, NFRs, success metrics")]
        if ui:
            steps.append(s("bmad-ux", "UX design: key screens and flows"))
        steps += [s("bmad-architecture", "architecture spine: stack, invariants, structure"),
                  s("bmad-create-epics-and-stories", "epics and user stories with acceptance criteria"),
                  s("bmad-sprint-planning", "readiness gate + sprint status for implementation"),
                  s("bmad-build", "implement stories one by one")]
        return RoutePlan(_pid(), request, "method", "large", "new product — full BMAD planning chain",
                         steps, router="heuristic")

    # ============================================================== execution
    def start(self, request: str, *, auto: bool = False, through: str = "plan",
              plan: RoutePlan | None = None) -> HandleResult:
        plan = plan or self.route(request, through)
        if auto:
            return self._run_auto(plan)
        return self._start_step(plan)

    def advance(self, plan: RoutePlan, turn: "TurnResult") -> HandleResult:
        """Interactive: call after every turn. Moves to the next step when the current one completes."""
        step = plan.current
        if step is None:
            return HandleResult(plan, turn, finished=True)
        if not self._step_done(step):
            return HandleResult(plan, turn)
        self._close(step, turn)
        if step.status == "completed" and step.skill in SHIP_SKILLS:
            step.verification = self.verify()
            if not step.verification["ok"] and step.fix_attempts < MAX_FIX_ATTEMPTS:
                step.status, step.fix_attempts, step.last_fix = "running", step.fix_attempts + 1, time.time()
                self.save(plan)
                fix = self.rt.send(step.session_id, self._fix_message(step))
                return HandleResult(plan, fix, note=f"verification failed — sent back to {step.agent} to fix "
                                                    f"(attempt {step.fix_attempts}/{MAX_FIX_ATTEMPTS})")
            if not step.verification["ok"]:
                step.status = "failed"
        self.save(plan)
        if step.status in {"blocked", "failed"}:
            return HandleResult(plan, turn, finished=True, note=f"{step.skill} {step.status}: {step.summary[:300]}")
        if plan.current is None:
            return HandleResult(plan, turn, finished=True, note="All plan steps completed.")
        return self._start_step(plan)

    def _step_done(self, step: PlanStep) -> bool:
        if not step.session_id:
            return False
        st = self.rt.workflow.state(self.rt.sessions.load(step.session_id))
        since = max(step.started or 0, step.last_fix or 0)
        for c in st.completed_skills:
            if (c.get("ts") or 0) < since or (c.get("result") or {}).get("implicit"):
                continue
            if c.get("skill") == step.skill or (step.last_fix and c.get("skill") in {None, step.agent}):
                return True
        return False

    def _start_step(self, plan: RoutePlan) -> HandleResult:
        self._expand_build(plan)
        step = plan.current
        if step is None:
            return HandleResult(plan, None, finished=True, note="All plan steps completed.")
        step.status, step.started = "running", time.time()
        turn = self.rt.start(agent=step.agent, skill=step.skill, message=self._brief(plan, step, headless=False),
                             headless=False, title=f"{plan.plan_id} · {step.skill}", metadata=self._meta(plan, step))
        step.session_id = turn.session_id
        self.save(plan)
        label = step.skill + (f" [{step.story}]" if step.story else "")
        return HandleResult(plan, turn, note=f"Step {plan.index(step)}/{len(plan.steps)}: {label}"
                                              f" with {step.agent or 'no persona'}")

    def _run_auto(self, plan: RoutePlan) -> HandleResult:
        last = None
        while True:
            self._expand_build(plan)
            step = plan.current
            if step is None:
                break
            step.status, step.started = "running", time.time()
            self.save(plan)
            last = self.rt.run_skill(step.skill, self._brief(plan, step, headless=True), agent=step.agent,
                                     headless=True, metadata=self._meta(plan, step))
            step.session_id = last.session_id
            self._close(step, last)
            if step.status == "completed" and step.skill in SHIP_SKILLS:
                step.verification = self.verify()
                while not step.verification["ok"] and step.fix_attempts < MAX_FIX_ATTEMPTS:
                    step.fix_attempts += 1
                    step.last_fix = time.time()
                    self.save(plan)
                    last = self.rt.send(step.session_id, self._fix_message(step))
                    step.verification = self.verify()
                if not step.verification["ok"]:
                    step.status = "failed"
                    step.summary = "verification failed: " + _failed_checks(step.verification)
            self.save(plan)
            if step.status in {"blocked", "failed"}:
                return HandleResult(plan, last, finished=True, note=f"stopped at {step.skill}: {step.summary[:300]}")
        return HandleResult(plan, last, finished=True, note="All plan steps completed.")

    def _meta(self, plan: RoutePlan, step: PlanStep) -> dict[str, Any]:
        i = plan.steps.index(step)
        nxt = next((s for s in plan.steps[i + 1:] if s.status == "pending"), None)
        return {"plan_id": plan.plan_id, "plan_step_skill": step.skill, "plan_story": step.story,
                "plan_next": f"`{nxt.skill}` with {nxt.agent or 'no persona'}" if nxt else "the end of the plan"}

    def _expand_build(self, plan: RoutePlan) -> None:
        """Replace a generic bmad-build step by one step per open sprint-status story (fresh session each)."""
        step = plan.current
        if step is None or step.skill != "bmad-build" or step.story is not None or step.status != "pending":
            return
        stories = self.open_stories()
        if not stories:
            return
        i = plan.steps.index(step)
        plan.steps[i:i + 1] = [PlanStep("bmad-build", step.agent, f"implement story {k}", story=k) for k in stories]
        self.save(plan)

    def open_stories(self) -> list[str]:
        path = Path(self.rt.config.variables().get("implementation_artifacts", "")) / "sprint-status.yaml"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s+(\d+-\d+-[A-Za-z0-9-]+):\s*([A-Za-z-]+)", line)
            if m and m.group(2).lower() != "done":
                out.append(m.group(1))
        return out

    # ============================================================ verification
    def verify(self) -> dict[str, Any]:
        """Install / build / test / start-up smoke check every package the project has. Independent of the LLM."""
        cx = self.rt.commands
        checks: list[dict[str, Any]] = []

        def record(res, label):
            checks.append({"check": label, "cmd": " ".join(res.command), "cwd": res.cwd, "ok": res.ok,
                           "exit": res.returncode, "tail": res.output[-2500:]})

        root = self.rt.project_root
        pkgs = [p for p in [root / "package.json", *sorted(root.glob("*/package.json"))]
                if p.is_file() and not any(x in p.parts for x in ("node_modules", "_bmad", "_bmad-output"))]
        for pkg in pkgs:
            d = pkg.parent
            try:
                meta = json.loads(pkg.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                checks.append({"check": "package.json", "cwd": str(d), "ok": False, "tail": f"invalid JSON: {exc}"})
                continue
            scripts = meta.get("scripts") or {}
            try:
                record(cx.run(["npm", "install"], str(d), timeout_s=900), "install")
                if checks[-1]["ok"] and "build" in scripts:
                    record(cx.run(["npm", "run", "build"], str(d), timeout_s=600), "build")
                if checks[-1]["ok"] and "test" in scripts and "no test specified" not in scripts["test"]:
                    record(cx.run(["npm", "test"], str(d), timeout_s=600), "test")
                entry = _server_entry(d, meta)
                if entry and all(c["ok"] for c in checks if c["cwd"] == str(d)):
                    record(cx.smoke_start(["node", entry], str(d)), f"start-up ({entry})")
            except Exception as exc:  # noqa: BLE001 - npm missing etc.
                checks.append({"check": "npm", "cwd": str(d), "ok": None, "tail": f"skipped: {exc}"})
        if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
            if (root / "tests").is_dir():
                try:
                    record(cx.run(["python", "-m", "pytest", "-q"], str(root)), "pytest")
                except Exception as exc:  # noqa: BLE001
                    checks.append({"check": "pytest", "cwd": str(root), "ok": None, "tail": f"skipped: {exc}"})
        ran = [c for c in checks if c["ok"] is not None]
        return {"ok": all(c["ok"] for c in ran), "checks": checks, "ts": time.time(),
                "note": "" if ran else "no runnable checks (no package.json / tests found, or npm missing)"}

    def _fix_message(self, step: PlanStep) -> str:
        return ("[orchestrator] Independent verification of your work FAILED. The checks below were run on the "
                "project. Fix the code/manifests, re-run the failing command with run_command until it passes, update "
                "sprint-status.yaml, then call complete_workflow again.\n\n" + _failed_checks(step.verification or {},
                                                                                      full=True))

    def _close(self, step: PlanStep, turn: "TurnResult") -> None:
        comp = turn.completion or {}
        cs = str(comp.get("status") or "")
        if cs in {"complete", "completed", "partial"}:
            step.status = "completed"
        elif cs == "blocked" or turn.status == "blocked":
            step.status = "blocked"
        elif turn.status == COMPLETED:
            step.status = "completed"
        else:                                # failed, or stopped mid-way (turn budget / waiting)
            step.status = "failed"
        step.summary = (comp.get("summary") or turn.error or turn.message or "")[:600]
        paths = list(comp.get("artifacts") or []) + [a["path"] for a in turn.artifacts]
        step.artifacts = list(dict.fromkeys(paths))

    def _brief(self, plan: RoutePlan, step: PlanStep, headless: bool) -> str:
        prior = [(s.skill, a) for s in plan.steps if s.status == "completed" for a in s.artifacts]
        prior_txt = "\n".join(f"- {sk}: {p}" for sk, p in prior) or "- none yet"
        lines = [f"User request: {plan.request}", "",
                 f"BMAD plan ({plan.track}): step {plan.index(step)} of {len(plan.steps)} — "
                 f"{step.skill}: {step.why}",
                 "Earlier steps produced (read them as inputs; they are in the project):", prior_txt]
        if step.story:
            lines += ["", f"Implement ONLY story `{step.story}` (see sprint-status.yaml and epics.md) in this session."]
        if STEP_DIRECTIVES.get(step.skill):
            lines += ["", "Planner directive: " + STEP_DIRECTIVES[step.skill]]
        if headless:
            lines += ["", "intent: create (or update if the artifact already exists). Work from the request and the "
                          "artifacts above; record anything inferred as an assumption."]
        return "\n".join(lines)

    # ============================================================ persistence
    def save(self, plan: RoutePlan) -> None:
        (self.plan_dir / f"{plan.plan_id}.json").write_text(json.dumps(plan.to_dict(), indent=1, ensure_ascii=False),
                                                            encoding="utf-8")

    def load(self, plan_id: str) -> RoutePlan:
        return RoutePlan.from_dict(json.loads((self.plan_dir / f"{plan_id}.json").read_text(encoding="utf-8")))

    def latest(self) -> RoutePlan | None:
        files = sorted(self.plan_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return self.load(files[0].stem) if files else None

    def plan_for_session(self, session_id: str) -> RoutePlan | None:
        for p in sorted(self.plan_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            plan = self.load(p.stem)
            if any(s.session_id == session_id for s in plan.steps):
                return plan
        return None

    # ================================================================ helpers
    def _skill_owners(self) -> dict[str, str | None]:
        owners: dict[str, list[str]] = {}
        for agent in self.rt.registry.list_agents():
            if agent.skill is None:
                continue
            try:
                menu = self.rt.config.customization(agent.skill.skill_dir, "agent").get("agent", {}).get("menu", [])
            except Exception:  # noqa: BLE001
                menu = []
            for item in menu:
                if item.get("skill"):
                    owners.setdefault(item["skill"], []).append(agent.agent_id)
        self._owners_all = owners
        primary: dict[str, str | None] = {sk: ags[0] for sk, ags in owners.items()}
        primary.update({k: v for k, v in CORE_OWNER.items() if k not in primary})
        if "bmad-sprint-planning" in owners and "bmad-agent-pm" in owners["bmad-sprint-planning"]:
            primary["bmad-sprint-planning"] = "bmad-agent-pm"
        return primary

    def _owners_of(self, skill: str) -> list[str]:
        return self._owners_all.get(skill, []) + ([CORE_OWNER[skill]] if CORE_OWNER.get(skill) else [])

    def _owner_view(self) -> dict[str, list[str]]:
        view: dict[str, list[str]] = {}
        for sk, ags in self._owners_all.items():
            for a in ags:
                view.setdefault(a, []).append(sk)
        return view

    def _order(self, steps: list[PlanStep]) -> list[PlanStep]:
        rank = {sk: i for i, sk in enumerate(["bmad-brainstorming", "bmad-forge-idea", "bmad-deep-recon",
                                              "bmad-product-brief", "bmad-prfaq", "bmad-prd", "bmad-ux",
                                              "bmad-architecture", "bmad-create-epics-and-stories",
                                              "bmad-sprint-planning", "bmad-spec", "bmad-build",
                                              "bmad-qa-generate-e2e-tests", "bmad-code-review",
                                              "bmad-retrospective"])}
        return sorted(steps, key=lambda s: rank.get(s.skill, 50))

    def _has_code(self) -> bool:
        root = self.rt.project_root
        markers = ("package.json", "pyproject.toml", "requirements.txt", "go.mod", "pom.xml", "Cargo.toml", "src")
        return any((root / m).exists() for m in markers)


def _server_entry(d: Path, meta: dict[str, Any]) -> str | None:
    deps = {**(meta.get("dependencies") or {}), **(meta.get("devDependencies") or {})}
    if not any(k in deps for k in ("express", "socket.io", "fastify", "koa", "@nestjs/core", "hapi", "@hapi/hapi")) \
            and not any(re.match(r"node\s", str(v)) for v in (meta.get("scripts") or {}).values()):
        return None
    for key in ("start", "server", "serve"):
        m = re.match(r"node\s+([^\s]+\.(?:m?js|cjs))", str((meta.get("scripts") or {}).get(key, "")))
        if m:
            return m.group(1)
    for cand in (meta.get("main"), "server.js", "index.js", "src/server.js", "src/index.js", "app.js"):
        if cand and (d / cand).is_file() and cand.endswith((".js", ".mjs", ".cjs")):
            return cand
    return None


def _failed_checks(v: dict[str, Any], full: bool = False) -> str:
    bad = [c for c in v.get("checks", []) if c.get("ok") is False]
    if not bad:
        return v.get("note", "")
    parts = []
    for c in bad:
        tail = c.get("tail", "")
        parts.append(f"- {c['check']} in {c['cwd']}: `{c.get('cmd', '')}` exit {c.get('exit')}"
                     + (f"\n```\n{tail[-2000:]}\n```" if full else ""))
    return "\n".join(parts)


def _pid() -> str:
    return time.strftime("plan-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def _json_from(text: str) -> Any:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


__all__ = ["Orchestrator", "RoutePlan", "PlanStep", "HandleResult"]
