from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillInfo:
    skill_id: str
    name: str
    description: str
    category: str
    module_code: str | None
    relative_path: str
    skill_file: Path
    customize_file: Path | None
    is_agent: bool


@dataclass(frozen=True)
class AgentInfo:
    agent_id: str
    name: str
    title: str
    description: str
    module_code: str | None
    skill_id: str | None
    skill_file: Path | None
    customize_file: Path | None
    manifest_file: Path | None


@dataclass(frozen=True)
class ManifestInfo:
    manifest_id: str
    manifest_type: str
    relative_path: str
    manifest_file: Path
    data: dict[str, Any]
    raw_content: str


@dataclass
class RegistrySnapshot:
    bmad_root: Path
    agents: list[AgentInfo]
    skills: list[SkillInfo]
    manifests: list[ManifestInfo]

    def summary(self) -> dict[str, Any]:
        def path_value(value: Path | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "bmad_root": str(self.bmad_root),
            "agent_count": len(self.agents),
            "skill_count": len(self.skills),
            "manifest_count": len(self.manifests),
            "agents": [
                {
                    "agent_id": x.agent_id,
                    "name": x.name,
                    "title": x.title,
                    "description": x.description,
                    "module_code": x.module_code,
                    "skill_id": x.skill_id,
                    "skill_file": path_value(x.skill_file),
                    "customize_file": path_value(x.customize_file),
                    "manifest_file": path_value(x.manifest_file),
                }
                for x in self.agents
            ],
            "skills": [
                {
                    "skill_id": x.skill_id,
                    "name": x.name,
                    "description": x.description,
                    "category": x.category,
                    "module_code": x.module_code,
                    "relative_path": x.relative_path,
                    "skill_file": str(x.skill_file),
                    "customize_file": path_value(x.customize_file),
                    "is_agent": x.is_agent,
                }
                for x in self.skills
            ],
            "manifests": [
                {
                    "manifest_id": x.manifest_id,
                    "manifest_type": x.manifest_type,
                    "relative_path": x.relative_path,
                    "manifest_file": str(x.manifest_file),
                }
                for x in self.manifests
            ],
        }


class BMadRegistry:
    MANIFEST_TYPES = {
        "module.yaml": "module",
        "module.yml": "module",
        "bmad-modules.yaml": "module_registry",
        "bmad-modules.yml": "module_registry",
        "bmad-manifest.json": "bmad_manifest",
        "manifest.json": "manifest",
        "manifest.yaml": "manifest",
        "manifest.yml": "manifest",
        "marketplace.json": "plugin_manifest",
        "customize.toml": "customization",
    }

    def __init__(
        self,
        bmad_root: str | Path | None = None,
        project_root: str | Path | None = None,
        extra_roots: list[str | Path] | None = None,
    ):
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root else Path(__file__).resolve().parents[1]
        )
        self.root = self._resolve_bmad_root(bmad_root)
        self.extra_roots = [
            Path(p).expanduser().resolve() for p in (extra_roots or [])
        ]
        self._snapshot: RegistrySnapshot | None = None

    def _resolve_bmad_root(self, configured_root: str | Path | None) -> Path:
        configured = configured_root or os.getenv("BMAD_METHOD_ROOT")
        if configured:
            root = Path(configured).expanduser().resolve()
            if not root.is_dir():
                raise FileNotFoundError(f"Configured BMAD directory does not exist: {root}")
            return root

        candidates = [
            self.project_root / "Plugins" / "BMAD-METHOD-6.12.4" / "BMAD-METHOD-6.12.4",
            self.project_root / "framework" / "BMAD-METHOD-6.12.4",
            self.project_root / "Plugins" / "BMAD-METHOD" / "BMAD-METHOD",
        ]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate.resolve()

        raise FileNotFoundError(
            "Could not locate BMAD Method. Checked:\n"
            + "\n".join(str(p) for p in candidates)
            + "\nSet BMAD_METHOD_ROOT to the actual BMAD directory."
        )

    def _get_resource_roots(self) -> list[Path]:
        candidates = [
            self.root / "src",
            self.project_root / "_bmad",
            *self.extra_roots,
        ]
        unique: dict[str, Path] = {}
        for candidate in candidates:
            if candidate.is_dir():
                resolved = candidate.resolve()
                unique[str(resolved)] = resolved
        return list(unique.values())

    @staticmethod
    def _parse_frontmatter(skill_file: Path) -> dict[str, str]:
        content = skill_file.read_text(encoding="utf-8-sig")
        if not content.startswith("---"):
            return {}
        end = content.find("\n---", 3)
        if end < 0:
            return {}
        fields: dict[str, str] = {}
        for line in content[3:end].splitlines():
            match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
            if match:
                value = match.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                fields[match.group(1)] = value
        return fields

    @staticmethod
    def _parse_simple_yaml(content: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for line in content.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*(?:#.*)?$", line)
            if not match:
                continue
            key, value = match.groups()
            value = value.strip()
            if value.startswith(("'", '"')):
                value = value.strip("'\"")
            elif value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            elif value.lower() in ("null", "~"):
                value = None
            result[key] = value
        return result

    @staticmethod
    def _parse_manifest_data(path: Path, content: str) -> dict[str, Any]:
        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                data = json.loads(content)
            elif suffix == ".toml":
                data = tomllib.loads(content)
            elif suffix in (".yaml", ".yml"):
                try:
                    import yaml
                    data = yaml.safe_load(content)
                except ImportError:
                    data = BMadRegistry._parse_simple_yaml(content)
            else:
                data = {}
        except Exception as exc:
            raise ValueError(f"Could not parse manifest {path}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _get_module_code(self, skill_file: Path) -> str | None:
        for parent in skill_file.parents:
            if parent == self.root.parent:
                break
            module_file = parent / "module.yaml"
            if module_file.is_file():
                try:
                    data = self._parse_manifest_data(
                        module_file, module_file.read_text(encoding="utf-8-sig")
                    )
                    code = data.get("code")
                    return str(code) if code else None
                except (OSError, ValueError):
                    return None
        return None

    def discover_manifests(self) -> list[ManifestInfo]:
        discovered: dict[str, ManifestInfo] = {}
        scan_roots = [self.root, *self._get_resource_roots()]
        unique_roots = {str(p.resolve()): p.resolve() for p in scan_roots if p.is_dir()}

        for scan_root in unique_roots.values():
            for path in scan_root.rglob("*"):
                if not path.is_file():
                    continue
                manifest_type = self.MANIFEST_TYPES.get(path.name.lower())
                if not manifest_type:
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(scan_root):
                    continue
                content = path.read_text(encoding="utf-8-sig")
                data = self._parse_manifest_data(path, content)
                relative = (
                    path.relative_to(self.root).as_posix()
                    if path.is_relative_to(self.root) else str(path)
                )
                discovered[str(resolved)] = ManifestInfo(
                    manifest_id=path.stem,
                    manifest_type=manifest_type,
                    relative_path=relative,
                    manifest_file=resolved,
                    data=data,
                    raw_content=content,
                )

        return sorted(
            discovered.values(),
            key=lambda x: (x.manifest_type, x.relative_path),
        )

    def discover_skills(self) -> list[SkillInfo]:
        discovered: dict[str, SkillInfo] = {}
        for resource_root in self._get_resource_roots():
            for candidate in resource_root.rglob("SKILL.md"):
                skill_file = candidate.resolve()
                if not skill_file.is_file() or not skill_file.is_relative_to(resource_root):
                    continue
                metadata = self._parse_frontmatter(skill_file)
                skill_id = metadata.get("name", skill_file.parent.name).strip()
                if not skill_id:
                    continue
                description = metadata.get("description", "").strip()
                relative = skill_file.parent.relative_to(resource_root)
                parts = relative.parts
                is_agent = "agents" in parts and parts.index("agents") + 1 < len(parts)
                category = "agent" if is_agent else (parts[0] if parts else "general")
                customize = skill_file.parent / "customize.toml"
                discovered[str(skill_file)] = SkillInfo(
                    skill_id=skill_id,
                    name=skill_id.replace("-", " ").title(),
                    description=description,
                    category=category,
                    module_code=self._get_module_code(skill_file),
                    relative_path=relative.as_posix(),
                    skill_file=skill_file,
                    customize_file=customize.resolve() if customize.is_file() else None,
                    is_agent=is_agent,
                )
        return sorted(discovered.values(), key=lambda x: (x.category, x.skill_id, x.relative_path))

    def _read_agent_roster(self, manifests: list[ManifestInfo]) -> dict[str, dict[str, Any]]:
        roster: dict[str, dict[str, Any]] = {}
        for manifest in manifests:
            if manifest.manifest_type != "module":
                continue
            agents = manifest.data.get("agents", [])
            if isinstance(agents, list):
                for agent in agents:
                    if not isinstance(agent, dict) or not agent.get("code"):
                        continue
                    agent_id = str(agent["code"])
                    roster[agent_id] = {
                        "name": agent.get("name", agent_id),
                        "title": agent.get("title", ""),
                        "description": agent.get("description", ""),
                        "module_code": manifest.data.get("code"),
                        "manifest_file": manifest.manifest_file,
                    }
                continue

            # Minimal YAML fallback for environments without PyYAML.
            match = re.search(
                r"(?ms)^agents:\s*\n(.*?)(?=^[A-Za-z_][A-Za-z0-9_-]*\s*:|\Z)",
                manifest.raw_content,
            )
            if not match:
                continue
            for block in re.split(r"(?m)^\s*-\s+code:\s*", match.group(1))[1:]:
                lines = block.splitlines()
                if not lines:
                    continue
                agent_id = lines[0].strip().strip("'\"")
                fields: dict[str, str] = {}
                for line in lines[1:]:
                    field_match = re.match(r"^\s+([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
                    if field_match:
                        fields[field_match.group(1)] = field_match.group(2).strip().strip("'\"")
                roster[agent_id] = {
                    "name": fields.get("name", agent_id),
                    "title": fields.get("title", ""),
                    "description": fields.get("description", ""),
                    "module_code": manifest.data.get("code"),
                    "manifest_file": manifest.manifest_file,
                }
        return roster

    def discover_agents(
        self,
        skills: list[SkillInfo] | None = None,
        manifests: list[ManifestInfo] | None = None,
    ) -> list[AgentInfo]:
        skills = self.discover_skills() if skills is None else skills
        manifests = self.discover_manifests() if manifests is None else manifests
        roster = self._read_agent_roster(manifests)
        agents: dict[str, AgentInfo] = {}

        for skill in skills:
            if not skill.is_agent:
                continue
            metadata = roster.get(skill.skill_id, {})
            agents[skill.skill_id] = AgentInfo(
                agent_id=skill.skill_id,
                name=metadata.get("name", skill.name),
                title=metadata.get("title", ""),
                description=metadata.get("description", skill.description),
                module_code=metadata.get("module_code") or skill.module_code,
                skill_id=skill.skill_id,
                skill_file=skill.skill_file,
                customize_file=skill.customize_file,
                manifest_file=metadata.get("manifest_file"),
            )

        for agent_id, metadata in roster.items():
            if agent_id not in agents:
                agents[agent_id] = AgentInfo(
                    agent_id=agent_id,
                    name=metadata["name"],
                    title=metadata["title"],
                    description=metadata["description"],
                    module_code=metadata["module_code"],
                    skill_id=None,
                    skill_file=None,
                    customize_file=None,
                    manifest_file=metadata["manifest_file"],
                )
        return sorted(agents.values(), key=lambda x: x.agent_id)

    def discover(self, refresh: bool = False) -> RegistrySnapshot:
        if self._snapshot is not None and not refresh:
            return self._snapshot
        manifests = self.discover_manifests()
        skills = self.discover_skills()
        agents = self.discover_agents(skills, manifests)
        self._snapshot = RegistrySnapshot(self.root, agents, skills, manifests)
        return self._snapshot

    def get_agent(self, agent_id: str) -> AgentInfo:
        matches = [x for x in self.discover().agents if x.agent_id == agent_id]
        if not matches:
            raise KeyError(f"Agent '{agent_id}' was not found.")
        if len(matches) > 1:
            raise ValueError(f"Agent ID '{agent_id}' is ambiguous.")
        return matches[0]

    def get_skill(
        self,
        skill_id: str,
        module_code: str | None = None,
        category: str | None = None,
    ) -> SkillInfo:
        matches = [x for x in self.discover().skills if x.skill_id == skill_id]
        if module_code is not None:
            matches = [x for x in matches if x.module_code == module_code]
        if category is not None:
            matches = [x for x in matches if x.category == category]
        if not matches:
            raise KeyError(
                f"Skill '{skill_id}' not found "
                f"(module_code={module_code!r}, category={category!r})."
            )
        if len(matches) > 1:
            locations = [f"{x.module_code or 'no-module'}:{x.category}:{x.relative_path}" for x in matches]
            raise ValueError(f"Skill ID '{skill_id}' is ambiguous: {locations}")
        return matches[0]

    def get_manifest(self, manifest_id: str) -> ManifestInfo:
        matches = [x for x in self.discover().manifests if x.manifest_id == manifest_id]
        if not matches:
            raise KeyError(f"Manifest '{manifest_id}' was not found.")
        if len(matches) > 1:
            raise ValueError(f"Manifest ID '{manifest_id}' is ambiguous.")
        return matches[0]

    def list_agents(self) -> list[AgentInfo]:
        return self.discover().agents

    def list_skills(self) -> list[SkillInfo]:
        return self.discover().skills

    def list_manifests(self) -> list[ManifestInfo]:
        return self.discover().manifests

    def list_agent_skills(self) -> list[SkillInfo]:
        return [x for x in self.list_skills() if x.is_agent]

    def list_regular_skills(self) -> list[SkillInfo]:
        return [x for x in self.list_skills() if not x.is_agent]

    def describe(self) -> dict[str, Any]:
        return self.discover().summary()
