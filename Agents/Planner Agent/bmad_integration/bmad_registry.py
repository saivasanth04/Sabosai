from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillInfo:
    skill_id: str
    name: str
    description: str
    category: str
    relative_path: str
    skill_file: Path


_FRONTMATTER_FIELD = re.compile(r"^(name|description):\s*(.*?)\s*$", re.MULTILINE)


def _frontmatter(path: Path) -> tuple[str, str]:
    """Read only the YAML frontmatter fields needed for discovery."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return path.parent.name, ""
    end = text.find("\n---", 3)
    if end < 0:
        return path.parent.name, ""
    block = text[3:end]
    values = dict(_FRONTMATTER_FIELD.findall(block))
    name = values.get("name", path.parent.name).strip().strip("\"'")
    description = values.get("description", "").strip().strip("\"'")
    return name, description


class BMadRegistry:
    """Discovers BMAD skills from the checked-out V6 repository; no skill list is hardcoded."""

    def __init__(self, bmad_root: str | Path | None = None):
        default_root = Path(__file__).resolve().parents[1] / "framework" / "BMAD-METHOD-6.12.4"
        configured = bmad_root or os.getenv("BMAD_METHOD_ROOT")
        self.root = Path(configured).expanduser().resolve() if configured else default_root.resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(
                f"BMAD repository not found at {self.root}. Set BMAD_METHOD_ROOT to the BMAD-METHOD-6.12.4 directory."
            )
        self.source_root = self.root / "src"

    def discover(self) -> list[SkillInfo]:
        if not self.source_root.is_dir():
            raise FileNotFoundError(f"BMAD source directory is missing: {self.source_root}")

        found: list[SkillInfo] = []
        for skill_file in self.source_root.rglob("SKILL.md"):
            rel = skill_file.relative_to(self.source_root)
            parts = rel.parts
            if len(parts) < 2:
                continue
            category = parts[0]
            skill_id, description = _frontmatter(skill_file)
            found.append(
                SkillInfo(
                    skill_id=skill_id,
                    name=skill_id.replace("-", " ").title(),
                    description=description,
                    category=category,
                    relative_path=rel.parent.as_posix(),
                    skill_file=skill_file.resolve(),
                )
            )

        # A duplicate ID is ambiguous and should not silently select an arbitrary file.
        ids = [item.skill_id for item in found]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"Duplicate BMAD skill IDs discovered: {', '.join(duplicates)}")

        return sorted(found, key=lambda item: (item.category, item.skill_id))

    def get(self, skill_id: str) -> SkillInfo:
        matches = [item for item in self.discover() if item.skill_id == skill_id]
        if not matches:
            available = ", ".join(item.skill_id for item in self.discover())
            raise KeyError(f"BMAD skill '{skill_id}' was not found. Discovered: {available}")
        return matches[0]

    def describe(self) -> list[dict[str, str]]:
        return [
            {
                "skill_id": item.skill_id,
                "name": item.name,
                "description": item.description,
                "category": item.category,
                "relative_path": item.relative_path,
            }
            for item in self.discover()
        ]
