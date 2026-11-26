from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .bmad_registry import BMadRegistry, SkillInfo


@dataclass(frozen=True)
class LoadedSkill:
    info: SkillInfo
    instructions: str
    customization: dict
    supporting_documents: dict[str, str]


_REFERENCE_PATTERN = re.compile(r"(?:references|assets)/[A-Za-z0-9_./-]+\.(?:md|txt|json|yaml|yml|toml)", re.IGNORECASE)


class BMadLoader:
    """Loads a skill and its local supporting resources without executing shell commands."""

    def __init__(self, registry: BMadRegistry):
        self.registry = registry

    @staticmethod
    def _safe_child(root: Path, relative: str) -> Path:
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root.resolve()):
            raise PermissionError(f"BMAD resource path escapes its skill directory: {relative}")
        return candidate

    def load(self, skill_id: str) -> LoadedSkill:
        info = self.registry.get(skill_id)
        skill_root = info.skill_file.parent.resolve()
        instructions = info.skill_file.read_text(encoding="utf-8")

        customization_path = skill_root / "customize.toml"
        customization = {}
        if customization_path.is_file():
            with customization_path.open("rb") as stream:
                customization = tomllib.load(stream)

        supporting: dict[str, str] = {}
        for relative in sorted(set(_REFERENCE_PATTERN.findall(instructions))):
            path = self._safe_child(skill_root, relative)
            if path.is_file():
                supporting[relative] = path.read_text(encoding="utf-8")

        return LoadedSkill(
            info=info,
            instructions=instructions,
            customization=customization,
            supporting_documents=supporting,
        )
