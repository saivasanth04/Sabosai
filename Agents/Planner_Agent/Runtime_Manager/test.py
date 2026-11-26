from pathlib import Path

from bmad_registry import BMadRegistry
from bmad_loader import BMadLoader


PROJECT_ROOT = Path(__file__).resolve().parents[1]

registry = BMadRegistry(
    project_root=PROJECT_ROOT,
)

loader = BMadLoader(
    registry=registry,
    project_root=PROJECT_ROOT,
)

# Load an agent
agent = loader.load_agent("bmad-agent-pm")

print(agent.name)
print(agent.instructions)

# Load a regular skill
skill = loader.load_skill("bmad-brainstorming")

print(skill.name)
print(skill.instructions)

# See all supporting resources loaded from references
for resource_path, resource in skill.resources.items():
    print(resource_path)

# Build the complete instruction context for an LLM
prompt = skill.to_prompt()

print(prompt)