from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class RouteDecision(BaseModel):
    """Validated workflow selection returned by the model."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="Short classification of the user's planning request.")
    selected_skill_ids: list[str] = Field(
        default_factory=list,
        description="Skill IDs selected from the supplied installed-skill registry. Never invent IDs.",
    )
    execution_order: list[str] = Field(
        default_factory=list,
        description="Selected skill IDs in the order they should be executed.",
    )
    rationale: str = Field(description="Why these skills are needed and why others were omitted.")
    project_inspection_required: bool = Field(
        default=True,
        description="True when the task concerns an existing codebase or existing project.",
    )
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


class PlannedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Relative output path beneath the configured planning output directory.")
    title: str = Field(description="Human-readable artifact title.")
    kind: Literal[
        "brief", "prd", "architecture", "ux", "epic", "story",
        "project_context", "research", "plan", "other"
    ] = "other"
    content: str = Field(description="Complete artifact content, normally Markdown.")


class SkillExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    artifacts: list[PlannedArtifact] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    unsupported_runtime_steps: list[str] = Field(
        default_factory=list,
        description="BMAD instructions that require native scripts or interactive features not provided by this adapter.",
    )
