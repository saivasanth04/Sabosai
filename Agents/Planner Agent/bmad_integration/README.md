# Sabosai BMAD integration adapter

This package is an initial, local BMAD V6 skill adapter for the Sabosai Planner Agent.

## Repository placement

Place this directory under:

`Agents/Planner Agent/bmad_integration/`

The default BMAD path expected by `BMadRegistry` is:

`Agents/Planner Agent/framework/BMAD-METHOD-6.12.4/`

You can override it with `BMAD_METHOD_ROOT`.

## Dependency

Install Pydantic v2:

```powershell
python -m pip install "pydantic>=2.7,<3"
```

The existing `LLM/client.py` is injected into the router and executor; this package does not create a second model client.

## Example

Run from `Agents/Planner Agent` so your existing `LLM` package is importable:

```python
from pathlib import Path

from LLM.client import GoModelClient
from bmad_integration.bmad_registry import BMadRegistry
from bmad_integration.bmad_loader import BMadLoader
from bmad_integration.bmad_router import BMadRouter
from bmad_integration.bmad_executor import BMadExecutor
from bmad_integration.bmad_artifacts import ArtifactStore
from bmad_integration.bmad_validator import BMadValidator

project_root = Path(r"C:\Projects\YourProject")
client = GoModelClient(model="router")

registry = BMadRegistry()
loader = BMadLoader(registry)
router = BMadRouter(client, registry)
store = ArtifactStore(project_root, output_dir="docs/planning")
validator = BMadValidator()
executor = BMadExecutor(client, loader, store, validator)

request = "Create a product requirements document and break the MVP into epics and stories."
decision = router.route(request, project_context="Existing repository summary goes here.")

for skill_id in decision.execution_order:
    result, written_paths = executor.execute(
        skill_id=skill_id,
        user_request=request,
        project_context="Verified repository summary goes here.",
        headless=True,
    )
    print(skill_id, result.summary, written_paths)
```

## Important compatibility boundary

This adapter discovers and loads actual `SKILL.md` files, their `customize.toml` files, and referenced Markdown/TOML/YAML/JSON resources. It uses GoModel for structured model output and writes artifacts safely inside the configured planning directory.

It deliberately does **not** execute arbitrary shell commands found in BMAD skill instructions. BMAD V6 skills may expect project-side `_bmad/scripts/` utilities, configuration resolution, memlog operations, interactive menus, or subagents. Those native runtime behaviors are not automatically provided by simply reading a `SKILL.md`; add and test explicit adapters for them before claiming full BMAD runtime compatibility.

The existing Sabosai GoModel client uses a fixed tool-call name (`product_requirements`) internally. That name is cosmetic for this adapter because the supplied JSON schema changes per call, but it should eventually be generalized to a configurable tool name without breaking existing callers.
