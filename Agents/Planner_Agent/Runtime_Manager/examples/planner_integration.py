"""How the NTG Planner Agent drives BMAD through the adapter.

Run from Agents/Planner_Agent/Runtime_Manager:
    set GOMODEL_API_KEY=sk-...
    set GOMODEL_MODEL=<your GoModel model alias>
    python examples\\planner_integration.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bmad_adapter import BMadRuntime  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2] / "Workspace" / "habit-tracker"


def planner_answers(question: dict) -> str:
    """Called whenever a headless BMAD skill hits a checkpoint (ask_user).

    Replace with your planner's own reasoning (another LLM call, a policy, a human queue…).
    """
    if question.get("options"):
        return question["options"][0]
    return "Use your best judgement and record it as an assumption."


def main() -> None:
    rt = BMadRuntime.for_project(PROJECT, answer_provider=planner_answers)
    print(json.dumps(rt.health(), indent=2, ensure_ascii=False))

    # 1) Headless: let BMAD produce a PRD from a brief and return its status JSON.
    prd = rt.run_skill("bmad-prd", {
        "intent": "create",
        "brief": "Mobile-first habit tracker for university students; streaks, reminders, "
                 "study-group accountability. Launch stakes. Web + Android.",
    })
    print("PRD:", prd.status, json.dumps(prd.completion, indent=2, ensure_ascii=False))

    # 2) Chain the BMAD Method sequence: architecture → epics & stories → sprint planning.
    for skill in ("bmad-architecture", "bmad-create-epics-and-stories", "bmad-sprint-planning"):
        r = rt.run_skill(skill, {"intent": "create", "inputs": "use the PRD in planning_artifacts"})
        print(skill, "→", r.status, (r.completion or {}).get("summary", "")[:200])
        if r.status != "completed":
            break

    print("Artifacts:", json.dumps(rt.artifacts.as_dicts(), indent=2, default=str)[:2000])

    # 3) Or expose BMAD to the planner's own LLM as tools:
    schemas, dispatch = rt.planner_tools()
    print([s["function"]["name"] for s in schemas])
    # ... add `schemas` to your planner's tool list, and route calls to dispatch(name, args)


if __name__ == "__main__":
    main()
