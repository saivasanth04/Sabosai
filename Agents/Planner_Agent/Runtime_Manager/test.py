from __future__ import annotations

import json
from pathlib import Path

from bmad_registry import BMadRegistry
from bmad_loader import BMadLoader


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    registry = BMadRegistry(project_root=PROJECT_ROOT)
    loader = BMadLoader(registry=registry, project_root=PROJECT_ROOT)

    snapshot = registry.discover(refresh=True)
    summary = registry.describe()

    # Confirms the registry summary is JSON serializable.
    json.dumps(summary, indent=2, ensure_ascii=False)

    print(f"BMAD root: {snapshot.bmad_root}")
    print(f"Agents: {len(snapshot.agents)}")
    print(f"Skills: {len(snapshot.skills)}")
    print(f"Manifests: {len(snapshot.manifests)}")

    print("\nFirst discovered agents:")
    for agent_info in snapshot.agents[:20]:
        print(f"  {agent_info.agent_id} -> {agent_info.skill_file}")

    print("\nFirst discovered regular skills:")
    for skill_info in registry.list_regular_skills()[:20]:
        print(f"  {skill_info.skill_id} [{skill_info.category}]")

    agent = loader.load_agent("bmad-agent-pm")
    print(f"\nLoaded agent: {agent.name}")
    print(f"Agent resources loaded: {len(agent.resources)}")

    skill = loader.load_skill("bmad-brainstorming")
    print(f"\nLoaded skill: {skill.name}")
    print(f"Skill resources loaded: {len(skill.resources)}")

    for resource_path in sorted(skill.resources):
        print(f"  {resource_path}")

    if skill.warnings:
        print("\nLoader warnings:")
        for warning in skill.warnings:
            print(f"  - {warning}")

    prompt = skill.to_prompt()
    print(f"\nGenerated prompt length: {len(prompt)} characters")

    # Exercises the public one-resource API when the skill has a reference.
    available = loader.list_resources(skill)
    if available:
        manually_loaded = loader.load_resource(skill, available[0])
        print(f"Manual resource load: {manually_loaded.relative_path}")

    print("\nBMAD Registry and Loader checks completed.")


if __name__ == "__main__":
    main()
