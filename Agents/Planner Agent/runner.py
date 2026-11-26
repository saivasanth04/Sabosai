from pathlib import Path

from LLM.client import GoModelClient

from bmad_integration.bmad_registry import BMadRegistry
from bmad_integration.bmad_loader import BMadLoader
from bmad_integration.bmad_router import BMadRouter
from bmad_integration.bmad_executor import BMadExecutor
from bmad_integration.bmad_artifacts import ArtifactStore
from bmad_integration.bmad_validator import BMadValidator

client = GoModelClient(model="router")

registry = BMadRegistry()
loader = BMadLoader(registry)

router = BMadRouter(client, registry)

store = ArtifactStore(
    project_root=Path(r"C:\Projects\YourProject"),
    output_dir="docs/planning"
)

validator = BMadValidator()

executor = BMadExecutor(
    client=client,
    loader=loader,
    artifact_store=store,
    validator=validator
)

request = "build a chess game with react and nodejs"

decision = router.route(
    user_request=request,
    project_context="Verified project context goes here."
)

for skill_id in decision.execution_order:
    result, paths = executor.execute(
        skill_id=skill_id,
        user_request=request,
        project_context="Verified project context goes here.",
        headless=True
    )

    print(result.summary)
    print(paths)