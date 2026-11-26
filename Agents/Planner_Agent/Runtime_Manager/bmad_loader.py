
from __future__ import annotations

import re
import tomllib

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------
# EXCEPTIONS
# ---------------------------------------------------------

class BMadLoaderError(Exception):
    """Base exception for BMAD loading errors."""


class BMadResourceNotFoundError(BMadLoaderError):
    """Raised when a requested BMAD resource does not exist."""


class BMadResourceSecurityError(BMadLoaderError):
    """Raised when a resource resolves outside its allowed root."""


class BMadResourceLimitError(BMadLoaderError):
    """Raised when resource loading exceeds configured limits."""


# ---------------------------------------------------------
# DATA MODELS
# ---------------------------------------------------------

@dataclass
class LoadedResource:
    name: str
    path: Path
    relative_path: str
    content: str
    resource_type: str
    size_bytes: int


@dataclass
class LoadedBMadSkill:
    skill_id: str
    name: str
    description: str
    category: str
    skill_root: Path
    skill_file: Path
    instructions: str

    resources: dict[str, LoadedResource] = field(
        default_factory=dict
    )

    customization: dict[str, Any] = field(
        default_factory=dict
    )

    customization_content: str = ""

    metadata: dict[str, str] = field(
        default_factory=dict
    )

    def resource_contents(self) -> dict[str, str]:
        return {
            name: resource.content
            for name, resource in self.resources.items()
        }

    def to_prompt(self) -> str:
        """
        Build a complete instruction string for an LLM.

        Resource files are included separately from the main
        instructions so their original paths remain identifiable.
        """

        sections = [
            f"# BMAD Skill: {self.name}",
            "",
            f"Skill ID: {self.skill_id}",
            f"Description: {self.description}",
            f"Category: {self.category}",
            "",
            "# Instructions",
            "",
            self.instructions,
        ]

        if self.customization:
            sections.extend([
                "",
                "# Customization",
                "",
                str(self.customization),
            ])

        if self.resources:
            sections.extend([
                "",
                "# Supporting Resources",
            ])

            for relative_path, resource in sorted(
                self.resources.items()
            ):
                sections.extend([
                    "",
                    f"## Resource: {relative_path}",
                    "",
                    resource.content,
                ])

        return "\n".join(sections)


# ---------------------------------------------------------
# LOADER
# ---------------------------------------------------------

class BMadLoader:

    TEXT_EXTENSIONS = {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".csv",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".py",
        ".sh",
        ".ps1",
        ".bat",
        ".sql",
        ".ini",
        ".cfg",
        ".conf",
    }

    RESOURCE_PATH_PATTERN = re.compile(
        r"""
        (?:
            \{skill-root\}/
            |
            \./
            |
            (?:references|assets|scripts|templates|workflows|steps)/
        )
        [^\s<>"'`]+
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    MARKDOWN_LINK_PATTERN = re.compile(
        r"""
        !?
        \[[^\]]*\]
        \(
        <?([^)\s>]+)>?
        (?:\s+["'][^"']*["'])?
        \)
        """,
        re.VERBOSE,
    )

    def __init__(
        self,
        registry,
        project_root: str | Path | None = None,
        max_resource_files: int = 100,
        max_file_bytes: int = 512_000,
        max_total_bytes: int = 2_000_000,
        max_reference_depth: int = 5,
    ):
        """
        Args:
            registry:
                Existing BMadRegistry instance.

            project_root:
                Project workspace used for {project-root} paths.

            max_resource_files:
                Maximum number of supporting files loaded.

            max_file_bytes:
                Maximum size of an individual resource.

            max_total_bytes:
                Maximum combined resource content size.

            max_reference_depth:
                Maximum recursive reference depth.
        """

        if registry is None:
            raise ValueError(
                "BMadLoader requires an initialized BMadRegistry."
            )

        self.registry = registry

        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root
            else registry.project_root.resolve()
        )

        self.max_resource_files = max_resource_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_reference_depth = max_reference_depth

    # -----------------------------------------------------
    # PUBLIC API: LOAD AGENT
    # -----------------------------------------------------

    def load_agent(
        self,
        agent_id: str,
        load_resources: bool = True,
    ) -> LoadedBMadSkill:

        agent = self.registry.get_agent(agent_id)

        if not agent.skill_file:
            raise BMadResourceNotFoundError(
                f"Agent '{agent_id}' exists in the registry, "
                "but it does not have an associated SKILL.md file."
            )

        return self.load_skill_file(
            skill_file=agent.skill_file,
            skill_id=agent.agent_id,
            name=agent.name,
            description=agent.description,
            category="agent",
            load_resources=load_resources,
        )

    # -----------------------------------------------------
    # PUBLIC API: LOAD SKILL
    # -----------------------------------------------------

    def load_skill(
        self,
        skill_id: str,
        load_resources: bool = True,
    ) -> LoadedBMadSkill:

        skill = self.registry.get_skill(skill_id)

        return self.load_skill_file(
            skill_file=skill.skill_file,
            skill_id=skill.skill_id,
            name=skill.name,
            description=skill.description,
            category=skill.category,
            load_resources=load_resources,
        )

    # -----------------------------------------------------
    # PUBLIC API: LOAD FROM FILE
    # -----------------------------------------------------

    def load_skill_file(
        self,
        skill_file: str | Path,
        skill_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        category: str = "general",
        load_resources: bool = True,
    ) -> LoadedBMadSkill:

        skill_file = Path(skill_file).expanduser().resolve()

        if not skill_file.is_file():
            raise BMadResourceNotFoundError(
                f"Skill file does not exist: {skill_file}"
            )

        if skill_file.name.lower() != "skill.md":
            raise BMadLoaderError(
                f"Expected a SKILL.md file, received: {skill_file}"
            )

        skill_root = skill_file.parent.resolve()

        instructions = self._read_text_file(skill_file)

        metadata = self._parse_frontmatter(instructions)

        resolved_id = (
            skill_id
            or metadata.get("name")
            or skill_root.name
        )

        resolved_name = (
            name
            or metadata.get("name")
            or resolved_id.replace("-", " ").title()
        )

        resolved_description = (
            description
            or metadata.get("description", "")
        )

        loaded = LoadedBMadSkill(
            skill_id=resolved_id,
            name=resolved_name,
            description=resolved_description,
            category=category,
            skill_root=skill_root,
            skill_file=skill_file,
            instructions=instructions,
            metadata=metadata,
        )

        self._load_customization(loaded)

        if load_resources:
            self._load_referenced_resources(loaded)

        return loaded

    # -----------------------------------------------------
    # PUBLIC API: LOAD ONE SUPPORTING RESOURCE
    # -----------------------------------------------------

    def load_resource(
        self,
        skill: LoadedBMadSkill,
        resource_path: str | Path,
    ) -> LoadedResource:

        path = self._resolve_resource_path(
            skill_root=skill.skill_root,
            resource_path=resource_path,
        )

        resource = self._read_resource(
            path=path,
            skill_root=skill.skill_root,
        )

        skill.resources[resource.relative_path] = resource

        return resource

    # -----------------------------------------------------
    # PUBLIC API: LIST RESOURCES
    # -----------------------------------------------------

    def list_resources(
        self,
        skill: LoadedBMadSkill,
        directories: Iterable[str] | None = None,
    ) -> list[str]:

        directories = directories or (
            "references",
            "assets",
            "scripts",
            "templates",
            "workflows",
            "steps",
        )

        discovered = []

        for directory in directories:

            root = (
                skill.skill_root / directory
            ).resolve()

            if not root.exists() or not root.is_dir():
                continue

            if not root.is_relative_to(skill.skill_root):
                raise BMadResourceSecurityError(
                    f"Directory escapes skill root: {root}"
                )

            for path in root.rglob("*"):

                if not path.is_file():
                    continue

                resolved = path.resolve()

                if not resolved.is_relative_to(skill.skill_root):
                    continue

                discovered.append(
                    resolved.relative_to(
                        skill.skill_root
                    ).as_posix()
                )

        return sorted(set(discovered))

    # -----------------------------------------------------
    # FRONTMATTER
    # -----------------------------------------------------

    @staticmethod
    def _parse_frontmatter(
        content: str,
    ) -> dict[str, str]:

        if not content.startswith("---"):
            return {}

        end = content.find("\n---", 3)

        if end == -1:
            return {}

        frontmatter = content[3:end]

        metadata = {}

        for line in frontmatter.splitlines():

            match = re.match(
                r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$",
                line,
            )

            if not match:
                continue

            key = match.group(1).strip()
            value = match.group(2).strip()

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ("'", '"')
            ):
                value = value[1:-1]

            metadata[key] = value

        return metadata

    # -----------------------------------------------------
    # CUSTOMIZATION
    # -----------------------------------------------------

    def _load_customization(
        self,
        skill: LoadedBMadSkill,
    ) -> None:

        customize_file = (
            skill.skill_root / "customize.toml"
        ).resolve()

        if not customize_file.exists():
            return

        if not customize_file.is_relative_to(
            skill.skill_root
        ):
            raise BMadResourceSecurityError(
                "Customization file resolves outside skill root."
            )

        content = self._read_text_file(customize_file)

        try:
            customization = tomllib.loads(content)

        except tomllib.TOMLDecodeError as exc:
            raise BMadLoaderError(
                f"Invalid customize.toml in "
                f"{skill.skill_root}: {exc}"
            ) from exc

        skill.customization = customization
        skill.customization_content = content

    # -----------------------------------------------------
    # REFERENCE DISCOVERY
    # -----------------------------------------------------

    def _extract_references(
        self,
        content: str,
    ) -> list[str]:

        references = set()

        # Markdown links and image references.
        for match in self.MARKDOWN_LINK_PATTERN.finditer(content):

            target = match.group(1).strip()

            if self._is_local_reference(target):
                references.add(target)

        # Explicit skill-relative paths.
        for match in self.RESOURCE_PATH_PATTERN.finditer(content):

            target = match.group(0).strip()

            target = target.strip(".,;:!?)]}")

            if self._is_local_reference(target):
                references.add(target)

        # Paths explicitly written inside inline code.
        for match in re.finditer(
            r"`([^`]+)`",
            content,
        ):

            target = match.group(1).strip()

            if self._is_local_reference(target):
                references.add(target)

        return sorted(references)

    @staticmethod
    def _is_local_reference(
        reference: str,
    ) -> bool:

        if not reference:
            return False

        reference = reference.strip()

        if reference.startswith(
            ("http://", "https://", "mailto:", "#", "data:")
        ):
            return False

        if reference.startswith(
            ("{project-root}", "{workflow.")
        ):
            return False

        if "://" in reference:
            return False

        if reference.startswith("$"):
            return False

        return True

    # -----------------------------------------------------
    # RECURSIVE RESOURCE LOADING
    # -----------------------------------------------------

    def _load_referenced_resources(
        self,
        skill: LoadedBMadSkill,
    ) -> None:

        visited = set()

        pending = [
            (reference, 1)
            for reference in self._extract_references(
                skill.instructions
            )
        ]

        total_bytes = 0

        while pending:

            reference, depth = pending.pop(0)

            if depth > self.max_reference_depth:
                continue

            try:
                paths = self._expand_reference(
                    skill_root=skill.skill_root,
                    reference=reference,
                )

            except BMadResourceNotFoundError:
                # External, project-specific, or optional references
                # can be unavailable in the local skill directory.
                # Keep the original instruction intact.
                continue

            for path in paths:

                resolved = path.resolve()

                if resolved in visited:
                    continue

                visited.add(resolved)

                if len(skill.resources) >= self.max_resource_files:
                    raise BMadResourceLimitError(
                        "Maximum supporting resource count exceeded: "
                        f"{self.max_resource_files}"
                    )

                resource = self._read_resource(
                    path=resolved,
                    skill_root=skill.skill_root,
                )

                total_bytes += resource.size_bytes

                if total_bytes > self.max_total_bytes:
                    raise BMadResourceLimitError(
                        "Maximum combined resource size exceeded: "
                        f"{self.max_total_bytes} bytes"
                    )

                skill.resources[
                    resource.relative_path
                ] = resource

                # Recursively load resources referenced by Markdown.
                if resolved.suffix.lower() in {
                    ".md",
                    ".markdown",
                }:

                    nested = self._extract_references(
                        resource.content
                    )

                    pending.extend(
                        (item, depth + 1)
                        for item in nested
                    )

    # -----------------------------------------------------
    # REFERENCE EXPANSION
    # -----------------------------------------------------

    def _expand_reference(
        self,
        skill_root: Path,
        reference: str,
    ) -> list[Path]:

        reference = reference.strip()

        reference = reference.replace(
            "{skill-root}/",
            "",
        )

        reference = reference.split("#", 1)[0]
        reference = reference.split("?", 1)[0]

        if not reference:
            return []

        if reference.startswith("{project-root}/"):
            raise BMadResourceNotFoundError(
                "Project-root resource is not a skill resource."
            )

        candidate = Path(reference)

        if candidate.is_absolute():
            raise BMadResourceSecurityError(
                f"Absolute resource paths are not allowed: {reference}"
            )

        # Prevent Windows-style traversal even on other platforms.
        normalized = reference.replace("\\", "/")

        if any(
            part == ".."
            for part in normalized.split("/")
        ):
            raise BMadResourceSecurityError(
                f"Parent-directory traversal is not allowed: {reference}"
            )

        matches = list(
            skill_root.glob(normalized)
        )

        resolved_matches = []

        for match in matches:

            resolved = match.resolve()

            if not resolved.is_relative_to(skill_root):
                raise BMadResourceSecurityError(
                    f"Resource escapes skill directory: {match}"
                )

            if resolved.is_dir():

                for child in resolved.rglob("*"):

                    if not child.is_file():
                        continue

                    child_resolved = child.resolve()

                    if not child_resolved.is_relative_to(
                        skill_root
                    ):
                        continue

                    if child_resolved.suffix.lower() in (
                        self.TEXT_EXTENSIONS
                    ):
                        resolved_matches.append(
                            child_resolved
                        )

                continue

            if resolved.is_file():
                resolved_matches.append(resolved)

        if not resolved_matches:
            raise BMadResourceNotFoundError(
                f"Supporting resource not found: {reference}"
            )

        return sorted(set(resolved_matches))

    # -----------------------------------------------------
    # RESOURCE READING
    # -----------------------------------------------------

    def _read_resource(
        self,
        path: Path,
        skill_root: Path,
    ) -> LoadedResource:

        path = path.resolve()

        if not path.is_relative_to(skill_root):
            raise BMadResourceSecurityError(
                f"Resource is outside skill directory: {path}"
            )

        if not path.is_file():
            raise BMadResourceNotFoundError(
                f"Resource does not exist: {path}"
            )

        suffix = path.suffix.lower()

        if suffix not in self.TEXT_EXTENSIONS:
            raise BMadLoaderError(
                f"Unsupported text resource type: {suffix}"
            )

        size = path.stat().st_size

        if size > self.max_file_bytes:
            raise BMadResourceLimitError(
                f"Resource exceeds individual file limit: {path}"
            )

        content = self._read_text_file(path)

        return LoadedResource(
            name=path.name,
            path=path,
            relative_path=path.relative_to(
                skill_root
            ).as_posix(),
            content=content,
            resource_type=suffix.lstrip("."),
            size_bytes=size,
        )

    # -----------------------------------------------------
    # FILE READING
    # -----------------------------------------------------

    @staticmethod
    def _read_text_file(
        path: Path,
    ) -> str:

        try:
            return path.read_text(
                encoding="utf-8-sig"
            )

        except UnicodeDecodeError as exc:
            raise BMadLoaderError(
                f"Could not decode text resource as UTF-8: {path}"
            ) from exc

        except OSError as exc:
            raise BMadLoaderError(
                f"Could not read resource: {path}: {exc}"
            ) from exc