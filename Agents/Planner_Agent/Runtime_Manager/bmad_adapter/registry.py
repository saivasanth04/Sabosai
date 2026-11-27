"""Registry — discover installed BMAD agents, skills and manifests.

Sources scanned (in order, first definition of a skill id wins):
  * ``{bmad_root}/src/**/SKILL.md``      the cloned BMAD-METHOD repo
  * ``settings.extra_skill_roots``        your own / third-party BMAD-style skills
  * ``{project_root}/_bmad/custom/skills`` project-local custom skills

Also read:
  * ``module.yaml``           module code, agent roster (name/title/icon/team)
  * ``module-help.csv``       the bmad-help catalog (phase, menu code, sequencing, outputs)
  * ``.claude-plugin/marketplace.json`` / ``bmad-manifest.json`` / ``bmad-modules.yaml``
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import _yaml
from .errors import BMadRegistryError

SKILL_FILE = "SKILL.md"
_SKIP_DIRS = {"node_modules", ".git", "test", "tests", "website", "docs", "web-bundles", "render", "__pycache__"}


@dataclass(frozen=True)
class HelpEntry:
    module: str
    skill: str
    display_name: str
    menu_code: str
    description: str
    action: str
    args: str
    phase: str
    preceded_by: str
    followed_by: str
    required: bool
    output_location: str
    outputs: str


@dataclass(frozen=True)
class SkillInfo:
    skill_id: str
    name: str
    description: str
    kind: str                       # agent | workflow | shim | utility
    category: str | None            # agents | plan | ship | core | v6-shims | custom
    module_code: str | None
    skill_dir: Path
    skill_file: Path
    customize_file: Path | None
    workflow_file: Path | None      # render-based skills (bmad-build style)
    uses_render: bool               # SKILL.md delegates to render_skill.py
    step_files: tuple[Path, ...]
    supports_headless: bool
    forwards_to: str | None         # v6 shims → target skill id
    installed: bool                 # listed in marketplace.json (or no manifest present)
    metadata: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    help: HelpEntry | None = None

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.skill_id,
            "kind": self.kind,
            "category": self.category,
            "module": self.module_code,
            "description": self.description,
            "menu_code": self.help.menu_code if self.help else None,
            "phase": self.help.phase if self.help else None,
            "headless": self.supports_headless,
            "forwards_to": self.forwards_to,
        }


@dataclass(frozen=True)
class AgentInfo:
    agent_id: str
    name: str
    title: str
    icon: str
    description: str
    team: str | None
    module_code: str | None
    skill: SkillInfo | None

    def brief(self) -> dict[str, Any]:
        return {"id": self.agent_id, "name": self.name, "title": self.title, "icon": self.icon,
                "team": self.team, "module": self.module_code, "description": self.description}


@dataclass(frozen=True)
class ManifestInfo:
    manifest_id: str
    manifest_type: str              # module | module_registry | plugin_manifest | bmad_manifest
    path: Path
    data: dict[str, Any] = field(hash=False, compare=False, default_factory=dict)


@dataclass
class RegistrySnapshot:
    bmad_root: Path
    agents: list[AgentInfo]
    skills: list[SkillInfo]
    manifests: list[ManifestInfo]
    modules: dict[str, dict[str, Any]]
    help_catalog: list[HelpEntry]

    def summary(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for s in self.skills:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        return {
            "bmad_root": str(self.bmad_root),
            "agent_count": len(self.agents),
            "skill_count": len(self.skills),
            "skills_by_kind": kinds,
            "manifest_count": len(self.manifests),
            "modules": sorted(self.modules),
        }


class BMadRegistry:
    """Discovers everything BMAD ships. Call :meth:`refresh` after adding skills."""

    def __init__(self, bmad_root: Path, project_root: Path | None = None,
                 extra_roots: Iterable[str | Path] = ()) -> None:
        self.bmad_root = Path(bmad_root).resolve()
        if not (self.bmad_root / "src").is_dir():
            raise BMadRegistryError(f"Not a BMAD-METHOD checkout (no src/): {self.bmad_root}")
        self.project_root = Path(project_root).resolve() if project_root else None
        self.extra_roots = [Path(p).expanduser().resolve() for p in extra_roots]
        self._snapshot: RegistrySnapshot | None = None

    # ------------------------------------------------------------------ public
    @property
    def snapshot(self) -> RegistrySnapshot:
        if self._snapshot is None:
            self._snapshot = self.refresh()
        return self._snapshot

    def refresh(self) -> RegistrySnapshot:
        manifests = self._scan_manifests()
        installed_ids = self._installed_ids(manifests)
        modules = self._scan_modules()
        help_catalog = self._scan_help()
        help_by_skill = {}
        for row in help_catalog:
            help_by_skill.setdefault(row.skill, row)

        skills: dict[str, SkillInfo] = {}
        for root, category_hint in self._skill_roots():
            for skill_file in sorted(_walk_skill_files(root)):
                info = self._build_skill(skill_file, root, category_hint, modules, installed_ids, help_by_skill)
                if info and info.skill_id not in skills:
                    skills[info.skill_id] = info

        agents = self._build_agents(modules, skills)
        self._snapshot = RegistrySnapshot(
            bmad_root=self.bmad_root,
            agents=agents,
            skills=sorted(skills.values(), key=lambda s: (s.kind != "agent", s.category or "", s.skill_id)),
            manifests=manifests,
            modules=modules,
            help_catalog=help_catalog,
        )
        return self._snapshot

    def list_skills(self, kind: str | None = None, include_shims: bool = True) -> list[SkillInfo]:
        out = [s for s in self.snapshot.skills if kind is None or s.kind == kind]
        if not include_shims:
            out = [s for s in out if s.kind != "shim"]
        return out

    def list_agents(self) -> list[AgentInfo]:
        return list(self.snapshot.agents)

    def get_skill(self, skill_id: str) -> SkillInfo:
        skill_id = skill_id.strip().removeprefix("skill:").split()[0]
        for s in self.snapshot.skills:
            if s.skill_id == skill_id:
                return s
        hit = self.find(skill_id)
        if hit:
            return hit
        raise BMadRegistryError(f"Unknown BMAD skill: {skill_id!r}")

    def get_agent(self, agent_ref: str) -> AgentInfo:
        ref = agent_ref.strip().lower()
        for a in self.snapshot.agents:
            if ref in {a.agent_id.lower(), a.name.lower(), a.title.lower(),
                       a.agent_id.lower().removeprefix("bmad-agent-")}:
                return a
        raise BMadRegistryError(f"Unknown BMAD agent: {agent_ref!r}")

    def find(self, query: str) -> SkillInfo | None:
        """Fuzzy lookup by id, short id, menu code or display name."""
        q = query.strip().lower()
        if not q:
            return None
        for s in self.snapshot.skills:
            candidates = {s.skill_id.lower(), s.skill_id.lower().removeprefix("bmad-")}
            if s.help:
                candidates |= {s.help.menu_code.lower(), s.help.display_name.lower()}
            if q in candidates:
                return s
        try:
            agent = self.get_agent(q)
            return agent.skill
        except BMadRegistryError:
            pass
        partial = [s for s in self.snapshot.skills if q in s.skill_id.lower()]
        return partial[0] if len(partial) == 1 else None

    def catalog_text(self) -> str:
        """Compact catalog for prompts (used by the instruction engine / planner)."""
        lines = ["| skill | kind | phase | code | description |", "|---|---|---|---|---|"]
        for s in self.snapshot.skills:
            if s.kind == "shim":
                continue
            desc = re.sub(r"\s+", " ", s.description)[:160]
            lines.append(f"| {s.skill_id} | {s.kind} | {s.help.phase if s.help else s.category or ''} | "
                         f"{s.help.menu_code if s.help else ''} | {desc} |")
        return "\n".join(lines)

    # --------------------------------------------------------------- internals
    def _skill_roots(self) -> list[tuple[Path, str | None]]:
        roots: list[tuple[Path, str | None]] = [(self.bmad_root / "src", None)]
        roots += [(r, "custom") for r in self.extra_roots if r.is_dir()]
        if self.project_root:
            custom = self.project_root / "_bmad" / "custom" / "skills"
            if custom.is_dir():
                roots.append((custom, "custom"))
        return roots

    def _build_skill(self, skill_file: Path, root: Path, category_hint: str | None,
                     modules: dict[str, dict[str, Any]], installed_ids: set[str] | None,
                     help_by_skill: dict[str, HelpEntry]) -> SkillInfo | None:
        try:
            text = skill_file.read_text(encoding="utf-8-sig")
        except OSError as exc:  # pragma: no cover
            raise BMadRegistryError(f"Cannot read {skill_file}: {exc}") from exc
        fm, body = _yaml.split_frontmatter(text)
        skill_dir = skill_file.parent
        skill_id = str(fm.get("name") or skill_dir.name)
        rel = skill_dir.relative_to(root).as_posix() if skill_dir.is_relative_to(root) else skill_dir.name
        parts = rel.split("/")
        category = category_hint or _category_from_parts(parts)
        metadata = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
        lifecycle = str(metadata.get("lifecycle", "")).lower()

        if category == "agents" or skill_id.startswith("bmad-agent-"):
            kind = "agent"
        elif lifecycle == "shim" or "v6-shims" in parts:
            kind = "shim"
        elif (skill_dir / "workflow.md").is_file() or list(skill_dir.glob("step-*.md")) or (skill_dir / "steps").is_dir():
            kind = "workflow"
        elif (skill_dir / "customize.toml").is_file():
            kind = "workflow"
        else:
            kind = "utility"

        forwards_to = None
        if kind == "shim":
            m = re.search(r"forwards to `?([a-z0-9-]+)`?", (fm.get("description") or "") + "\n" + body[:1500])
            forwards_to = m.group(1) if m else None

        step_files = tuple(sorted(
            [p for p in skill_dir.glob("step-*.md")] + [p for p in (skill_dir / "steps").glob("*.md")]
            if (skill_dir / "steps").is_dir() else skill_dir.glob("step-*.md")
        ))
        module_code = _module_for(skill_dir, root)
        if module_code is None and category in {"agents", "plan", "ship"}:
            module_code = "bmm"
        return SkillInfo(
            skill_id=skill_id,
            name=skill_id,
            description=str(fm.get("description") or "").strip(),
            kind=kind,
            category=category,
            module_code=module_code,
            skill_dir=skill_dir.resolve(),
            skill_file=skill_file.resolve(),
            customize_file=(skill_dir / "customize.toml").resolve() if (skill_dir / "customize.toml").is_file() else None,
            workflow_file=(skill_dir / "workflow.md").resolve() if (skill_dir / "workflow.md").is_file() else None,
            uses_render="render_skill.py" in body,
            step_files=step_files,
            supports_headless=(skill_dir / "references" / "headless.md").is_file() or "headless" in body.lower(),
            forwards_to=forwards_to,
            installed=installed_ids is None or skill_id in installed_ids or category == "custom",
            metadata={"frontmatter": fm, "relative_path": rel},
            help=help_by_skill.get(skill_id),
        )

    def _scan_modules(self) -> dict[str, dict[str, Any]]:
        modules: dict[str, dict[str, Any]] = {}
        for path in sorted((self.bmad_root / "src").rglob("module.yaml")):
            if _skip(path):
                continue
            data = _yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
            if isinstance(data, dict) and data.get("code"):
                data["_path"] = str(path)
                data["_dir"] = str(path.parent)
                modules[str(data["code"])] = data
        return modules

    def _scan_help(self) -> list[HelpEntry]:
        rows: list[HelpEntry] = []
        for path in sorted((self.bmad_root / "src").rglob("module-help.csv")):
            if _skip(path):
                continue
            reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")))
            for r in reader:
                if not r.get("skill") or r.get("skill") == "_meta":
                    continue
                rows.append(HelpEntry(
                    module=r.get("module", ""), skill=r.get("skill", ""),
                    display_name=r.get("display-name", ""), menu_code=r.get("menu-code", ""),
                    description=r.get("description", ""), action=r.get("action", ""),
                    args=r.get("args", ""), phase=r.get("phase", ""),
                    preceded_by=r.get("preceded-by", ""), followed_by=r.get("followed-by", ""),
                    required=str(r.get("required", "")).lower() == "true",
                    output_location=r.get("output-location", ""), outputs=r.get("outputs", ""),
                ))
        return rows

    def _scan_manifests(self) -> list[ManifestInfo]:
        found: list[ManifestInfo] = []
        mp = self.bmad_root / ".claude-plugin" / "marketplace.json"
        if mp.is_file():
            found.append(ManifestInfo("marketplace", "plugin_manifest", mp, _read_json(mp)))
        mods = self.bmad_root / "bmad-modules.yaml"
        if mods.is_file():
            found.append(ManifestInfo("bmad-modules", "module_registry", mods,
                                      _yaml.safe_load(mods.read_text(encoding="utf-8")) or {}))
        for path in sorted((self.bmad_root / "src").rglob("bmad-manifest.json")):
            found.append(ManifestInfo(path.parent.name, "bmad_manifest", path, _read_json(path)))
        for path in sorted((self.bmad_root / "src").rglob("module.yaml")):
            if not _skip(path):
                found.append(ManifestInfo(path.parent.name, "module", path, {}))
        return found

    def _installed_ids(self, manifests: list[ManifestInfo]) -> set[str] | None:
        for m in manifests:
            if m.manifest_type == "plugin_manifest":
                ids: set[str] = set()
                for plugin in m.data.get("plugins", []):
                    for skill_path in plugin.get("skills", []):
                        ids.add(Path(skill_path).name)
                return ids or None
        return None

    def _build_agents(self, modules: dict[str, dict[str, Any]], skills: dict[str, SkillInfo]) -> list[AgentInfo]:
        agents: dict[str, AgentInfo] = {}
        for code, mod in modules.items():
            for entry in mod.get("agents") or []:
                if not isinstance(entry, dict) or not entry.get("code"):
                    continue
                aid = str(entry["code"])
                agents[aid] = AgentInfo(
                    agent_id=aid, name=str(entry.get("name", aid)), title=str(entry.get("title", "")),
                    icon=str(entry.get("icon", "")), description=str(entry.get("description", "")),
                    team=entry.get("team") or code, module_code=code, skill=skills.get(aid),
                )
        for s in skills.values():           # agents present on disk but not in a roster
            if s.kind == "agent" and s.skill_id not in agents:
                m = re.search(r"^#\s+(.+?)\s+[—-]\s+(.+)$", s.skill_file.read_text(encoding="utf-8"), re.M)
                agents[s.skill_id] = AgentInfo(
                    agent_id=s.skill_id, name=m.group(1) if m else s.skill_id,
                    title=m.group(2) if m else "", icon="", description=s.description,
                    team=s.module_code, module_code=s.module_code, skill=s,
                )
        return sorted(agents.values(), key=lambda a: a.agent_id)


# ---------------------------------------------------------------------- helpers
def _walk_skill_files(root: Path):
    for path in root.rglob(SKILL_FILE):
        if not _skip(path.relative_to(root)):
            yield path


def _skip(path: Path) -> bool:
    return any(part in _SKIP_DIRS for part in path.parts[:-1])


def _category_from_parts(parts: list[str]) -> str | None:
    for p in parts:
        if p in {"agents", "plan", "ship", "v6-shims"}:
            return p
    if parts and parts[0] == "core-skills":
        return "core"
    return parts[0] if len(parts) > 1 else None


def _module_for(skill_dir: Path, root: Path) -> str | None:
    for parent in [skill_dir, *skill_dir.parents]:
        my = parent / "module.yaml"
        if my.is_file():
            m = re.search(r"^code:\s*([A-Za-z0-9_-]+)", my.read_text(encoding="utf-8"), re.M)
            return m.group(1) if m else None
        if parent == root:
            break
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
