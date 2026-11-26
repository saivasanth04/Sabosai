
from __future__ import annotations

import json
import os
import re
import tomllib

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------
# DATA MODELS
# ---------------------------------------------------------

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
        return {
            "bmad_root": str(self.bmad_root),
            "agent_count": len(self.agents),
            "skill_count": len(self.skills),
            "manifest_count": len(self.manifests),
            "agents": [asdict(x) for x in self.agents],
            "skills": [asdict(x) for x in self.skills],
            "manifests": [
                {
                    "manifest_id": x.manifest_id,
                    "manifest_type": x.manifest_type,
                    "relative_path": x.relative_path,
                }
                for x in self.manifests
            ],
        }


# ---------------------------------------------------------
# REGISTRY
# ---------------------------------------------------------

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
            if project_root
            else Path(__file__).resolve().parents[1]
        )

        self.root = self._resolve_bmad_root(bmad_root)

        self.extra_roots = [
            Path(path).expanduser().resolve()
            for path in (extra_roots or [])
        ]

        self._snapshot: RegistrySnapshot | None = None

    # -----------------------------------------------------
    # ROOT RESOLUTION
    # -----------------------------------------------------

    def _resolve_bmad_root(
        self,
        configured_root: str | Path | None,
    ) -> Path:

        configured = (
            configured_root
            or os.getenv("BMAD_METHOD_ROOT")
        )

        if configured:
            root = Path(configured).expanduser().resolve()

            if not root.is_dir():
                raise FileNotFoundError(
                    f"Configured BMAD directory does not exist: {root}"
                )

            return root

        candidates = [
            self.project_root
            / "Plugins"
            / "BMAD-METHOD-6.12.4"
            / "BMAD-METHOD-6.12.4",

            self.project_root
            / "framework"
            / "BMAD-METHOD-6.12.4",

            self.project_root
            / "Plugins"
            / "BMAD-METHOD"
            / "BMAD-METHOD",
        ]

        for candidate in candidates:
            if candidate.is_dir():
                return candidate.resolve()

        raise FileNotFoundError(
            "Could not locate BMAD Method.\n"
            "Expected BMAD at:\n"
            + "\n".join(str(path) for path in candidates)
            + "\nSet BMAD_METHOD_ROOT to the actual BMAD directory."
        )

    # -----------------------------------------------------
    # RESOURCE ROOTS
    # -----------------------------------------------------

    def _get_resource_roots(self) -> list[Path]:

        roots = []

        source = self.root / "src"

        if source.is_dir():
            roots.append(source)

        # Also support an installed BMAD project.
        installed = self.project_root / "_bmad"

        if installed.is_dir():
            roots.append(installed)

        # User-provided additional skill/agent directories.
        roots.extend(
            path for path in self.extra_roots
            if path.is_dir()
        )

        # Remove duplicate roots.
        unique = {}

        for path in roots:
            unique[str(path.resolve())] = path.resolve()

        return list(unique.values())

    # -----------------------------------------------------
    # FRONTMATTER
    # -----------------------------------------------------

    @staticmethod
    def _parse_frontmatter(
        skill_file: Path,
    ) -> dict[str, str]:

        content = skill_file.read_text(
            encoding="utf-8-sig"
        )

        if not content.startswith("---"):
            return {}

        end = content.find("\n---", 3)

        if end == -1:
            return {}

        block = content[3:end]

        fields = {}

        for line in block.splitlines():
            match = re.match(
                r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$",
                line,
            )

            if not match:
                continue

            key = match.group(1).strip()
            value = match.group(2).strip()

            # Remove simple YAML quotes.
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ("'", '"')
            ):
                value = value[1:-1]

            fields[key] = value

        return fields

    # -----------------------------------------------------
    # MANIFEST PARSING
    # -----------------------------------------------------

    @staticmethod
    def _parse_manifest_data(
        path: Path,
        content: str,
    ) -> dict[str, Any]:

        suffix = path.suffix.lower()

        try:
            if suffix == ".json":
                data = json.loads(content)
                return data if isinstance(data, dict) else {}

            if suffix == ".toml":
                return tomllib.loads(content)

            # YAML parsing is optional. Registry discovery itself
            # does not require PyYAML.
            if suffix in (".yaml", ".yml"):
                try:
                    import yaml

                    data = yaml.safe_load(content)
                    return data if isinstance(data, dict) else {}

                except ImportError:
                    return BMadRegistry._parse_simple_yaml(content)

        except Exception as exc:
            raise ValueError(
                f"Could not parse manifest {path}: {exc}"
            ) from exc

        return {}

    @staticmethod
    def _parse_simple_yaml(content: str) -> dict[str, Any]:
        """
        Standard-library fallback for basic YAML metadata.

        The complete original YAML remains available in raw_content.
        """

        result = {}

        for line in content.splitlines():
            if not line or line.startswith("#"):
                continue

            match = re.match(
                r"^([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*(?:#.*)?$",
                line,
            )

            if not match:
                continue

            key = match.group(1)
            value = match.group(2).strip()

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

    # -----------------------------------------------------
    # MODULE METADATA
    # -----------------------------------------------------

    def _get_module_code(self, skill_file: Path) -> str | None:

        for parent in skill_file.parents:
            if parent == self.root.parent:
                break

            module_file = parent / "module.yaml"

            if module_file.is_file():
                content = module_file.read_text(
                    encoding="utf-8-sig"
                )

                match = re.search(
                    r"(?m)^code:\s*[\"']?([^\"'\s#]+)",
                    content,
                )

                if match:
                    return match.group(1)

                return None

        return None

    # -----------------------------------------------------
    # MANIFEST DISCOVERY
    # -----------------------------------------------------

    def discover_manifests(self) -> list[ManifestInfo]:

        discovered = {}

        # Scan the BMAD repository itself.
        scan_roots = [self.root]

        # Scan installed project BMAD resources and extra roots.
        scan_roots.extend(self._get_resource_roots()[1:])

        for scan_root in scan_roots:

            if not scan_root.is_dir():
                continue

            for path in scan_root.rglob("*"):

                if not path.is_file():
                    continue

                manifest_type = self.MANIFEST_TYPES.get(
                    path.name.lower()
                )

                if not manifest_type:
                    continue

                resolved = path.resolve()

                if not resolved.is_relative_to(scan_root.resolve()):
                    continue

                try:
                    content = path.read_text(
                        encoding="utf-8-sig"
                    )

                    data = self._parse_manifest_data(
                        path,
                        content,
                    )

                    relative = path.relative_to(
                        self.root
                    ).as_posix() if path.is_relative_to(self.root) else str(path)

                    discovered[str(resolved)] = ManifestInfo(
                        manifest_id=path.stem,
                        manifest_type=manifest_type,
                        relative_path=relative,
                        manifest_file=resolved,
                        data=data,
                        raw_content=content,
                    )

                except (OSError, ValueError):
                    raise

        return sorted(
            discovered.values(),
            key=lambda item: (
                item.manifest_type,
                item.relative_path,
            ),
        )

    # -----------------------------------------------------
    # SKILL DISCOVERY
    # -----------------------------------------------------

    def discover_skills(self) -> list[SkillInfo]:

        discovered = {}

        for resource_root in self._get_resource_roots():

            for skill_file in resource_root.rglob("SKILL.md"):

                skill_file = skill_file.resolve()

                if not skill_file.is_file():
                    continue

                if not skill_file.is_relative_to(resource_root):
                    continue

                metadata = self._parse_frontmatter(
                    skill_file
                )

                skill_id = metadata.get(
                    "name",
                    skill_file.parent.name,
                ).strip()

                description = metadata.get(
                    "description",
                    "",
                ).strip()

                if not skill_id:
                    continue

                relative = skill_file.parent.relative_to(
                    resource_root
                )

                parts = relative.parts

                is_agent = (
                    "agents" in parts
                    and parts.index("agents") + 1 < len(parts)
                )

                if is_agent:
                    category = "agent"
                elif parts:
                    category = parts[0]
                else:
                    category = "general"

                customize = (
                    skill_file.parent / "customize.toml"
                )

                if not customize.is_file():
                    customize = None

                module_code = self._get_module_code(
                    skill_file
                )

                info = SkillInfo(
                    skill_id=skill_id,
                    name=skill_id.replace(
                        "-", " "
                    ).title(),
                    description=description,
                    category=category,
                    module_code=module_code,
                    relative_path=relative.as_posix(),
                    skill_file=skill_file,
                    customize_file=(
                        customize.resolve()
                        if customize
                        else None
                    ),
                    is_agent=is_agent,
                )

                discovered[str(skill_file)] = info

        return sorted(
            discovered.values(),
            key=lambda item: (
                item.category,
                item.skill_id,
            ),
        )

    # -----------------------------------------------------
    # AGENT DISCOVERY
    # -----------------------------------------------------

    def _read_agent_roster(
        self,
        manifests: list[ManifestInfo],
    ) -> dict[str, dict[str, Any]]:

        roster = {}

        for manifest in manifests:

            if manifest.manifest_type != "module":
                continue

            agents = manifest.data.get("agents", [])

            if isinstance(agents, list):

                for agent in agents:

                    if not isinstance(agent, dict):
                        continue

                    agent_id = agent.get("code")

                    if not agent_id:
                        continue

                    roster[agent_id] = {
                        "name": agent.get("name", agent_id),
                        "title": agent.get("title", ""),
                        "description": agent.get("description", ""),
                        "module_code": manifest.data.get("code"),
                        "manifest_file": manifest.manifest_file,
                    }

                continue

            # Standard-library fallback when PyYAML is absent.
            # Reads simple agent roster blocks from module.yaml.
            content = manifest.raw_content
            match = re.search(
                r"(?ms)^agents:\s*\n(.*?)(?=^[A-Za-z_][A-Za-z0-9_-]*\s*:|\Z)",
                content,
            )

            if not match:
                continue

            for block in re.split(
                r"(?m)^\s*-\s+code:\s*",
                match.group(1),
            )[1:]:

                fields = {}

                for line in block.splitlines():

                    field = re.match(
                        r"^\s*([A-Za-z0-9_-]+):\s*(.*?)\s*$",
                        line,
                    )

                    if field:
                        value = field.group(2).strip().strip("'\"")
                        fields[field.group(1)] = value

                agent_id = block.splitlines()[0].strip().strip("'\"")

                if not agent_id:
                    continue

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

        skills = skills if skills is not None else self.discover_skills()

        manifests = (
            manifests
            if manifests is not None
            else self.discover_manifests()
        )

        roster = self._read_agent_roster(manifests)

        agents = {}

        # Discover agents from actual agent SKILL.md files.
        for skill in skills:

            if not skill.is_agent:
                continue

            agent_id = skill.skill_id

            metadata = roster.get(agent_id, {})

            agents[agent_id] = AgentInfo(
                agent_id=agent_id,
                name=metadata.get(
                    "name",
                    skill.name,
                ),
                title=metadata.get("title", ""),
                description=metadata.get(
                    "description",
                    skill.description,
                ),
                module_code=(
                    metadata.get("module_code")
                    or skill.module_code
                ),
                skill_id=skill.skill_id,
                skill_file=skill.skill_file,
                customize_file=skill.customize_file,
                manifest_file=metadata.get("manifest_file"),
            )

        # Also expose agents declared by module manifests.
        # They may not have a corresponding SKILL.md yet.
        for agent_id, metadata in roster.items():

            if agent_id in agents:
                continue

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

        return sorted(
            agents.values(),
            key=lambda item: item.agent_id,
        )

    # -----------------------------------------------------
    # FULL REGISTRY
    # -----------------------------------------------------

    def discover(self, refresh: bool = False) -> RegistrySnapshot:

        if self._snapshot is not None and not refresh:
            return self._snapshot

        manifests = self.discover_manifests()
        skills = self.discover_skills()

        agents = self.discover_agents(
            skills=skills,
            manifests=manifests,
        )

        self._snapshot = RegistrySnapshot(
            bmad_root=self.root,
            agents=agents,
            skills=skills,
            manifests=manifests,
        )

        return self._snapshot

    # -----------------------------------------------------
    # LOOKUP METHODS
    # -----------------------------------------------------

    def get_agent(self, agent_id: str) -> AgentInfo:

        snapshot = self.discover()

        matches = [
            item for item in snapshot.agents
            if item.agent_id == agent_id
        ]

        if not matches:
            raise KeyError(
                f"Agent '{agent_id}' was not found."
            )

        if len(matches) > 1:
            raise ValueError(
                f"Agent ID '{agent_id}' is ambiguous."
            )

        return matches[0]

    def get_skill(self, skill_id: str) -> SkillInfo:

        snapshot = self.discover()

        matches = [
            item for item in snapshot.skills
            if item.skill_id == skill_id
        ]

        if not matches:
            raise KeyError(
                f"Skill '{skill_id}' was not found."
            )

        if len(matches) > 1:
            raise ValueError(
                f"Skill ID '{skill_id}' is ambiguous. "
                "Use the module/category to disambiguate."
            )

        return matches[0]

    def get_manifest(
        self,
        manifest_id: str,
    ) -> ManifestInfo:

        snapshot = self.discover()

        matches = [
            item for item in snapshot.manifests
            if item.manifest_id == manifest_id
        ]

        if not matches:
            raise KeyError(
                f"Manifest '{manifest_id}' was not found."
            )

        if len(matches) > 1:
            raise ValueError(
                f"Manifest ID '{manifest_id}' is ambiguous."
            )

        return matches[0]

    # -----------------------------------------------------
    # CONVENIENCE METHODS
    # -----------------------------------------------------

    def list_agents(self) -> list[AgentInfo]:
        return self.discover().agents

    def list_skills(self) -> list[SkillInfo]:
        return self.discover().skills

    def list_manifests(self) -> list[ManifestInfo]:
        return self.discover().manifests

    def list_agent_skills(self) -> list[SkillInfo]:
        return [
            skill for skill in self.list_skills()
            if skill.is_agent
        ]

    def list_regular_skills(self) -> list[SkillInfo]:
        return [
            skill for skill in self.list_skills()
            if not skill.is_agent
        ]

    def describe(self) -> dict[str, Any]:
        return self.discover().summary()
