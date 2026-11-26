from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from .bmad_models import PlannedArtifact, SkillExecutionResult


class BMadValidator:
    """Checks artifact safety and basic planning quality before files are persisted."""

    _PLACEHOLDERS = re.compile(r"\{\{[^}]+\}\}|<TODO>|\bINSERT HERE\b", re.IGNORECASE)
    _FR_ID = re.compile(r"\bFR-(\d+)\b", re.IGNORECASE)

    def validate_result(self, result: SkillExecutionResult) -> list[str]:
        errors: list[str] = []
        if not result.summary.strip():
            errors.append("Execution summary is empty.")
        if not result.artifacts:
            errors.append("No planning artifacts were produced.")

        seen: set[str] = set()
        for artifact in result.artifacts:
            errors.extend(self.validate_artifact(artifact))
            normalized = Path(artifact.path).as_posix().casefold()
            if normalized in seen:
                errors.append(f"Duplicate artifact path: {artifact.path}")
            seen.add(normalized)
        return errors

    def validate_artifact(self, artifact: PlannedArtifact) -> list[str]:
        errors = []
        posix_path = PurePosixPath(artifact.path.replace("\\", "/"))
        windows_path = PureWindowsPath(artifact.path)
        if (posix_path.is_absolute() or windows_path.is_absolute()
                or windows_path.drive or ".." in posix_path.parts
                or ".." in windows_path.parts):
            errors.append(f"Unsafe artifact path: {artifact.path}")
        if not artifact.path.strip():
            errors.append("Artifact path is empty.")
        if not artifact.title.strip():
            errors.append(f"Artifact title is empty: {artifact.path}")
        if not artifact.content.strip():
            errors.append(f"Artifact content is empty: {artifact.path}")
        if self._PLACEHOLDERS.search(artifact.content):
            errors.append(f"Unresolved template placeholder found in {artifact.path}.")
        if artifact.kind == "prd":
            ids = self._FR_ID.findall(artifact.content)
            if len(ids) != len(set(ids)):
                errors.append(f"Duplicate FR identifiers found in {artifact.path}.")
        return errors

    def require_valid(self, result: SkillExecutionResult) -> None:
        errors = self.validate_result(result)
        if errors:
            raise ValueError("BMAD artifact validation failed:\n- " + "\n- ".join(errors))
