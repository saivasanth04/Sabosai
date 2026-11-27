"""Loader — load SKILL.md instructions and supporting resources.

BMAD's own rule is *just-in-time loading*: "never load multiple step files
simultaneously". So the loader returns the skill's instructions plus a **resource
index** (paths + sizes), not every file's content. The LLM reads step files /
templates on demand through the ``read_file`` tool, which goes back through
:meth:`BMadLoader.read_resource` for the security and size checks.

Path tokens bound at load time (what an IDE does implicitly):
  ``{project-root}`` → absolute project root
  ``{skill-root}``   → absolute skill directory (or render snapshot dir)
  ``{skill-name}``   → skill directory basename

Render-based skills (``bmad-build``, ``bmad-build-auto``) delegate to
``_bmad/scripts/render_skill.py``; the loader runs it and loads the printed
``workflow.md`` snapshot as the instructions, exactly as the SKILL.md demands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import _yaml
from .errors import (BMadLoaderError, BMadResourceLimitError, BMadResourceNotFoundError,
                     BMadResourceSecurityError)
from .registry import SkillInfo
from .settings import LoaderSettings

if TYPE_CHECKING:  # pragma: no cover
    from .config_resolver import ConfigResolver
    from .registry import AgentInfo, BMadRegistry
    from .script_executor import ScriptExecutor

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".csv", ".json", ".toml", ".yaml", ".yml", ".html", ".htm",
                 ".xml", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".sql", ".ini", ".cfg"}
_RESOURCE_KIND = {"references": "reference", "assets": "asset", "scripts": "script", "templates": "template",
                  "steps": "step", "agents": "subagent-prompt", "review-prompts": "review-prompt", "types": "type"}


@dataclass
class ResourceRef:
    relative_path: str
    path: Path
    kind: str
    size_bytes: int


@dataclass
class LoadedResource:
    relative_path: str
    path: Path
    content: str
    kind: str
    size_bytes: int


@dataclass
class LoadedBMadSkill:
    info: SkillInfo
    frontmatter: dict[str, Any]
    instructions: str                    # SKILL.md (or rendered workflow.md) with path tokens bound
    skill_root: Path                     # where bare paths resolve (snapshot dir for render skills)
    entry_file: Path
    resources: list[ResourceRef] = field(default_factory=list)
    customization: dict[str, Any] = field(default_factory=dict)
    rendered: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def skill_id(self) -> str:
        return self.info.skill_id

    def resource_index(self) -> str:
        if not self.resources:
            return "_No supporting resources._"
        lines = []
        for r in self.resources:
            lines.append(f"- `{r.path}` ({r.kind}, {r.size_bytes} B)")
        return "\n".join(lines)

    def to_prompt(self) -> str:
        parts = [f"# BMAD Skill: {self.skill_id}",
                 f"Description: {self.info.description}",
                 f"Skill root (`{{skill-root}}`): `{self.skill_root}`",
                 f"Entry file: `{self.entry_file}`", "",
                 "## Instructions (follow exactly)", "", self.instructions.strip(), "",
                 "## Supporting resources (read on demand with read_file — do NOT preload step files)",
                 self.resource_index()]
        if self.warnings:
            parts += ["", "## Loader warnings", *(f"- {w}" for w in self.warnings)]
        return "\n".join(parts)


class BMadLoader:
    def __init__(self, registry: "BMadRegistry", project_root: Path, config: "ConfigResolver",
                 scripts: "ScriptExecutor", limits: LoaderSettings | None = None) -> None:
        if registry is None:
            raise ValueError("BMadLoader requires an initialized BMadRegistry.")
        self.registry = registry
        self.project_root = Path(project_root).resolve()
        self.config = config
        self.scripts = scripts
        self.limits = limits or LoaderSettings()

    # ------------------------------------------------------------------ load
    def load_skill(self, skill_ref: str | SkillInfo) -> LoadedBMadSkill:
        info = skill_ref if isinstance(skill_ref, SkillInfo) else self.registry.get_skill(skill_ref)
        if not info.skill_file.is_file():
            raise BMadResourceNotFoundError(f"Skill file does not exist: {info.skill_file}")
        text = info.skill_file.read_text(encoding="utf-8-sig")
        fm, body = _yaml.split_frontmatter(text)
        warnings: list[str] = []
        customization: dict[str, Any] = {}
        if info.customize_file:
            try:
                customization = self.config.customization(info.skill_dir)
            except Exception as exc:  # resolver failure must never block a load
                warnings.append(f"customization resolve failed, defaults used: {exc}")

        if info.uses_render and info.workflow_file:
            return self._load_rendered(info, fm, customization, warnings)

        instructions = self._bind(body, info.skill_dir, info.skill_dir.name)
        return LoadedBMadSkill(
            info=info, frontmatter=fm, instructions=instructions, skill_root=info.skill_dir,
            entry_file=info.skill_file, resources=self._index(info.skill_dir), customization=customization,
            warnings=warnings,
        )

    def load_agent(self, agent: "AgentInfo | str") -> LoadedBMadSkill:
        info = self.registry.get_agent(agent) if isinstance(agent, str) else agent
        if info.skill is None:
            raise BMadLoaderError(f"Agent '{info.agent_id}' is registered but has no SKILL.md file.")
        return self.load_skill(info.skill)

    def _load_rendered(self, info: SkillInfo, fm: dict, customization: dict, warnings: list[str]) -> LoadedBMadSkill:
        script = self.config.scripts_dir / "render_skill.py"
        if not script.exists():
            self.config.ensure_installed()
        res = self.scripts.run(script, ["--project-root", str(self.project_root), "--skill", str(info.skill_dir)])
        m = re.search(r"read and follow (.+workflow\.md)\s*$", res.stdout.strip())
        if not res.ok or not m:
            raise BMadLoaderError(f"render_skill.py failed for {info.skill_id}: {res.stdout.strip()} {res.stderr.strip()}")
        entry = Path(m.group(1).strip()).resolve()
        snapshot_root = entry.parent
        instructions = self._bind(entry.read_text(encoding="utf-8"), snapshot_root, info.skill_dir.name)
        resources = self._index(snapshot_root) + [r for r in self._index(info.skill_dir) if not r.relative_path.endswith(".md")]
        return LoadedBMadSkill(
            info=info, frontmatter=fm, instructions=instructions, skill_root=snapshot_root, entry_file=entry,
            resources=resources, customization=customization, rendered=True, warnings=warnings,
        )

    # --------------------------------------------------------------- resources
    def read_resource(self, path: str | Path, skill: LoadedBMadSkill | None = None,
                      reader: "Callable[[Path], str] | None" = None) -> LoadedResource:
        p = Path(str(path))
        if not p.is_absolute():
            if skill is None:
                raise BMadResourceNotFoundError(f"relative resource path needs a skill context: {path}")
            p = skill.skill_root / p
            if not p.exists() and skill.skill_root != skill.info.skill_dir:
                p = skill.info.skill_dir / path
        p = p.resolve()
        roots = [self.registry.bmad_root, self.project_root, *self.registry.extra_roots]
        if not any(p == r or p.is_relative_to(r) for r in roots):
            raise BMadResourceSecurityError(f"resource resolves outside permitted roots: {p}")
        if not p.is_file():
            raise BMadResourceNotFoundError(f"resource not found: {p}")
        size = p.stat().st_size
        if size > self.limits.max_file_bytes:
            raise BMadResourceLimitError(f"{p.name} is {size} B (limit {self.limits.max_file_bytes} B)")
        if p.suffix.lower() not in TEXT_SUFFIXES:
            raise BMadLoaderError(f"not a text resource: {p.name}")
        content = reader(p) if reader else p.read_text(encoding="utf-8-sig", errors="replace")
        if skill is not None and (p.is_relative_to(skill.skill_root) or p.is_relative_to(skill.info.skill_dir)):
            content = self._bind(content, skill.skill_root, skill.info.skill_dir.name)
        else:
            content = content.replace("{project-root}", str(self.project_root))
        kind = _kind_for(p, skill.skill_root if skill else p.parent)
        return LoadedResource(str(p), p, content, kind, size)

    def _index(self, root: Path) -> list[ResourceRef]:
        refs: list[ResourceRef] = []
        total = 0
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name in {"SKILL.md", "manifest.json"} or "__pycache__" in p.parts \
                    or "tests" in p.relative_to(root).parts:
                continue
            size = p.stat().st_size
            total += size
            if len(refs) >= self.limits.max_resource_files:
                break
            refs.append(ResourceRef(p.relative_to(root).as_posix(), p.resolve(), _kind_for(p, root), size))
        return refs

    def _bind(self, text: str, skill_root: Path, skill_name: str) -> str:
        return (text.replace("{project-root}", str(self.project_root))
                    .replace("{skill-root}", str(skill_root))
                    .replace("{skill-name}", skill_name))


def _kind_for(path: Path, root: Path) -> str:
    try:
        first = path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        first = ""
    if path.name.startswith("step-"):
        return "step"
    if path.name == "workflow.md":
        return "workflow"
    if path.name == "customize.toml":
        return "customization"
    return _RESOURCE_KIND.get(first, "file")
