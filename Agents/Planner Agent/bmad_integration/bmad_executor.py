from __future__ import annotations

from .bmad_artifacts import ArtifactStore
from .bmad_loader import BMadLoader
from .bmad_models import SkillExecutionResult
from .bmad_validator import BMadValidator


class BMadExecutor:
    """
    Executes a loaded BMAD skill as model guidance and persists structured outputs.

    This adapter does not execute arbitrary shell commands embedded in SKILL.md.
    BMAD scripts such as resolve_config.py and memlog.py must be explicitly
    integrated in a later compatibility layer if full native behavior is required.
    """

    def __init__(
        self,
        client,
        loader: BMadLoader,
        artifact_store: ArtifactStore,
        validator: BMadValidator | None = None,
    ):
        self.client = client
        self.loader = loader
        self.artifact_store = artifact_store
        self.validator = validator or BMadValidator()

    def execute(
        self,
        skill_id: str,
        user_request: str,
        project_context: str = "",
        prior_artifacts: str = "",
        headless: bool = True,
    ) -> tuple[SkillExecutionResult, list[str]]:
        skill = self.loader.load(skill_id)

        supporting_text = "\n\n".join(
            f"### Supporting resource: {name}\n{content}"
            for name, content in skill.supporting_documents.items()
        )
        customization_text = str(skill.customization) if skill.customization else "(none)"

        system = (
            "You are executing one installed BMAD V6 skill inside a custom LangGraph Planner. "
            "Follow the supplied SKILL.md and supporting resources as the methodology source. "
            "Do not invent BMAD commands, files, templates, roles, or requirements. "
            "Do not claim to have run a script, inspected a file, researched the web, or used an MCP "
            "tool unless evidence is present in the supplied context. "
            "This is a headless execution: do not stop for interactive menus or wait for user input. "
            "Represent missing information as assumptions or unresolved_questions. "
            "Only produce artifacts justified by the request and available evidence. "
            "Artifact paths must be relative to the configured planning output directory. "
            "Return complete artifact contents, not outlines or placeholders. "
            "If the BMAD instructions require runtime scripts or capabilities unavailable here, "
            "list them in unsupported_runtime_steps rather than pretending they ran."
        )

        user = (
            f"BMAD SKILL ID: {skill_id}\n"
            f"SKILL DESCRIPTION: {skill.info.description}\n\n"
            f"SKILL.md INSTRUCTIONS:\n{skill.instructions}\n\n"
            f"CUSTOMIZATION (TOML parsed to data):\n{customization_text}\n\n"
            f"SUPPORTING DOCUMENTS:\n{supporting_text or '(none discovered)'}\n\n"
            f"USER REQUEST:\n{user_request}\n\n"
            f"VERIFIED PROJECT CONTEXT:\n{project_context[:20000] or '(not supplied)'}\n\n"
            f"EXISTING PLANNING ARTIFACTS:\n{prior_artifacts[:12000] or '(none supplied)'}\n\n"
            f"EXECUTION MODE: {'headless' if headless else 'interactive-adapted'}"
        )

        raw = self.client.complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            schema=SkillExecutionResult.model_json_schema(),
        )
        result = SkillExecutionResult.model_validate(raw)
        self.validator.require_valid(result)
        paths = self.artifact_store.write_many(result.artifacts)
        return result, [str(path) for path in paths]
