from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


class BMadLoaderError(Exception):
    """Base exception for BMAD loading errors."""


class BMadResourceNotFoundError(BMadLoaderError):
    """A requested BMAD resource could not be found."""


class BMadResourceSecurityError(BMadLoaderError):
    """A resource attempted to resolve outside its permitted skill directory."""


class BMadResourceLimitError(BMadLoaderError):
    """A configured resource loading limit was exceeded."""


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
    resources: dict[str, LoadedResource] = field(default_factory=dict)
    customization: dict[str, Any] = field(default_factory=dict)
    customization_content: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def resource_contents(self) -> dict[str, str]:
        return {key: value.content for key, value in self.resources.items()}

    def to_prompt(self) -> str:
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
        if self.customization_content:
            sections.extend(["", "# Customization", "", self.customization_content])
        if self.resources:
            sections.extend(["", "# Supporting Resources"])
            for relative_path, resource in sorted(self.resources.items()):
                sections.extend(["", f"## Resource: {relative_path}", "", resource.content])
        if self.warnings:
            sections.extend(["", "# Loader Warnings", ""])
            sections.extend(f"- {warning}" for warning in self.warnings)
        return "\n".join(sections)


class BMadLoader:
    TEXT_EXTENSIONS = {
        ".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".toml",
        ".xml", ".csv", ".html", ".htm", ".css", ".js", ".mjs", ".cjs",
        ".ts", ".tsx", ".jsx", ".py", ".sh", ".ps1", ".bat", ".sql",
        ".ini", ".cfg", ".conf",
    }
    RESOURCE_DIRS = ("references", "assets", "scripts", "templates", "workflows", "steps")
    RESOURCE_PATH_PATTERN = re.compile(
        r"""(?:\{skill-root\}/|\./|(?:references|assets|scripts|templates|workflows|steps)/)[^\s<>"'`]+""",
        re.IGNORECASE,
    )
    MARKDOWN_LINK_PATTERN = re.compile(
        r"""!?\[[^\]]*\]\(<?([^)\s>]+)>?(?:\s+["'][^"']*["'])?\)""",
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
        if registry is None:
            raise ValueError("BMadLoader requires an initialized BMadRegistry.")
        self.registry = registry
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root else registry.project_root.resolve()
        )
        self.max_resource_files = max_resource_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_reference_depth = max_reference_depth

    def load_agent(self, agent_id: str, load_resources: bool = True) -> LoadedBMadSkill:
        agent = self.registry.get_agent(agent_id)
        if not agent.skill_file:
            raise BMadResourceNotFoundError(
                f"Agent '{agent_id}' is registered but has no SKILL.md file."
            )
        return self.load_skill_file(
            agent.skill_file,
            skill_id=agent.agent_id,
            name=agent.name,
            description=agent.description,
            category="agent",
            load_resources=load_resources,
        )

    def load_skill(
        self,
        skill_id: str,
        load_resources: bool = True,
        module_code: str | None = None,
        category: str | None = None,
    ) -> LoadedBMadSkill:
        skill = self.registry.get_skill(skill_id, module_code=module_code, category=category)
        return self.load_skill_file(
            skill.skill_file,
            skill_id=skill.skill_id,
            name=skill.name,
            description=skill.description,
            category=skill.category,
            load_resources=load_resources,
        )

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
            raise BMadResourceNotFoundError(f"Skill file does not exist: {skill_file}")
        if skill_file.name.lower() != "skill.md":
            raise BMadLoaderError(f"Expected a SKILL.md file, received: {skill_file}")

        allowed_roots = [self.registry.root.resolve(), *self.registry._get_resource_roots()]
        if not any(skill_file.is_relative_to(root) for root in allowed_roots):
            raise BMadResourceSecurityError(
                f"SKILL.md is outside the configured BMAD/resource roots: {skill_file}"
            )

        skill_root = skill_file.parent.resolve()
        instructions = self._read_text_file(skill_file)
        metadata = self._parse_frontmatter(instructions)
        resolved_id = skill_id or metadata.get("name") or skill_root.name
        loaded = LoadedBMadSkill(
            skill_id=resolved_id,
            name=name or metadata.get("name") or resolved_id.replace("-", " ").title(),
            description=description or metadata.get("description", ""),
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

    def load_resource(self, skill: LoadedBMadSkill, resource_path: str | Path) -> LoadedResource:
        path = self._resolve_resource_path(skill.skill_root, resource_path)
        resource = self._read_resource(path, skill.skill_root)
        skill.resources[resource.relative_path] = resource
        return resource

    def list_resources(
        self,
        skill: LoadedBMadSkill,
        directories: Iterable[str] | None = None,
    ) -> list[str]:
        result: set[str] = set()
        for directory in directories or self.RESOURCE_DIRS:
            root = self._resolve_resource_path(skill.skill_root, directory, allow_missing=True)
            if root is None or not root.is_dir():
                continue
            for candidate in root.rglob("*"):
                if candidate.is_file():
                    resolved = candidate.resolve()
                    if not resolved.is_relative_to(skill.skill_root):
                        raise BMadResourceSecurityError(f"Resource escapes skill root: {candidate}")
                    result.add(resolved.relative_to(skill.skill_root).as_posix())
        return sorted(result)

    @staticmethod
    def _parse_frontmatter(content: str) -> dict[str, str]:
        if not content.startswith("---"):
            return {}
        end = content.find("\n---", 3)
        if end < 0:
            return {}
        metadata: dict[str, str] = {}
        for line in content[3:end].splitlines():
            match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
            if match:
                value = match.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                metadata[match.group(1)] = value
        return metadata

    def _load_customization(self, skill: LoadedBMadSkill) -> None:
        customize_file = (skill.skill_root / "customize.toml").resolve()
        if not customize_file.exists():
            return
        if not customize_file.is_relative_to(skill.skill_root):
            raise BMadResourceSecurityError("Customization file resolves outside skill root.")
        content = self._read_text_file(customize_file)
        try:
            skill.customization = tomllib.loads(content)
            skill.customization_content = content
        except tomllib.TOMLDecodeError as exc:
            raise BMadLoaderError(f"Invalid customize.toml in {skill.skill_root}: {exc}") from exc

    @staticmethod
    def _is_local_reference(reference: str) -> bool:
        value = reference.strip()
        if not value or value.startswith(("http://", "https://", "mailto:", "#", "data:", "$")):
            return False
        if value.startswith(("{project-root}", "{workflow.")) or "://" in value:
            return False
        return True

    def _extract_references(self, content: str) -> list[tuple[str, bool]]:
        references: set[tuple[str, bool]] = set()
        for match in self.MARKDOWN_LINK_PATTERN.finditer(content):
            target = match.group(1).strip()
            if self._is_local_reference(target):
                references.add((target, True))
        for match in self.RESOURCE_PATH_PATTERN.finditer(content):
            target = match.group(0).strip().rstrip(".,;:!?)]}")
            if self._is_local_reference(target):
                references.add((target, False))
        for match in re.finditer(r"`([^`]+)`", content):
            target = match.group(1).strip()
            if self._is_local_reference(target) and (
                "/" in target or "\\" in target
            ):
                references.add((target, True))
        return sorted(references)

    def _load_referenced_resources(self, skill: LoadedBMadSkill) -> None:
        # Each queued item carries the document containing the reference so
        # ordinary Markdown links resolve relative to that document.
        queue: list[tuple[Path, str, bool, int]] = [
            (skill.skill_file, ref, relative_link, 1)
            for ref, relative_link in self._extract_references(skill.instructions)
        ]
        visited: set[Path] = set()
        total_bytes = 0

        while queue:
            source_file, reference, relative_link, depth = queue.pop(0)
            if depth > self.max_reference_depth:
                skill.warnings.append(
                    f"Reference depth limit reached for '{reference}' from {source_file.name}."
                )
                continue
            try:
                paths = self._expand_reference(
                    skill.skill_root, reference, source_file, relative_link
                )
            except BMadResourceNotFoundError as exc:
                skill.warnings.append(str(exc))
                continue

            for path in paths:
                resolved = path.resolve()
                if resolved in visited:
                    continue
                visited.add(resolved)
                if len(skill.resources) >= self.max_resource_files:
                    raise BMadResourceLimitError(
                        f"Maximum supporting resource count exceeded ({self.max_resource_files})."
                    )
                resource = self._read_resource(resolved, skill.skill_root)
                total_bytes += resource.size_bytes
                if total_bytes > self.max_total_bytes:
                    raise BMadResourceLimitError(
                        f"Combined supporting resources exceed {self.max_total_bytes} bytes."
                    )
                skill.resources[resource.relative_path] = resource
                if resolved.suffix.lower() in {".md", ".markdown"}:
                    queue.extend(
                        (resolved, ref, is_link, depth + 1)
                        for ref, is_link in self._extract_references(resource.content)
                    )

    def _resolve_resource_path(
        self,
        skill_root: Path,
        resource_path: str | Path,
        allow_missing: bool = False,
    ) -> Path | None:
        raw = str(resource_path).strip().replace("\\", "/")
        if raw.startswith("{skill-root}/"):
            raw = raw[len("{skill-root}/"):]
        raw = raw.split("#", 1)[0].split("?", 1)[0]
        if not raw:
            raise BMadResourceNotFoundError("Empty resource path.")
        candidate = Path(raw)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:", raw):
            raise BMadResourceSecurityError(f"Absolute resource paths are not allowed: {raw}")
        if any(part == ".." for part in raw.split("/")):
            raise BMadResourceSecurityError(f"Parent-directory traversal is not allowed: {raw}")
        resolved = (skill_root / candidate).resolve()
        if not resolved.is_relative_to(skill_root.resolve()):
            raise BMadResourceSecurityError(f"Resource escapes skill root: {raw}")
        if not resolved.exists():
            if allow_missing:
                return None
            raise BMadResourceNotFoundError(f"Supporting resource not found: {raw}")
        return resolved

    def _expand_reference(
        self,
        skill_root: Path,
        reference: str,
        source_file: Path,
        relative_link: bool,
    ) -> list[Path]:
        raw = reference.strip()
        if raw.startswith("{project-root}/"):
            raise BMadResourceNotFoundError(
                f"Project-root reference is not loaded as a skill resource: {raw}"
            )
        raw = raw.replace("{skill-root}/", "")
        raw = raw.split("#", 1)[0].split("?", 1)[0]
        raw = raw.replace("\\", "/")
        if not raw:
            return []
        if Path(raw).is_absolute() or re.match(r"^[A-Za-z]:", raw):
            raise BMadResourceSecurityError(f"Absolute resource paths are not allowed: {raw}")
        if any(part == ".." for part in raw.split("/")):
            raise BMadResourceSecurityError(f"Parent-directory traversal is not allowed: {raw}")

        # Markdown links are relative to the Markdown file; explicit BMAD
        # resource directory paths and {skill-root} paths are skill-root-relative.
        base = source_file.parent if relative_link else skill_root
        candidate_pattern = (base / raw).resolve()
        if not candidate_pattern.is_relative_to(skill_root):
            raise BMadResourceSecurityError(f"Resource escapes skill root: {raw}")

        matches = list(candidate_pattern.parent.glob(candidate_pattern.name))
        results: set[Path] = set()
        for match in matches:
            resolved = match.resolve()
            if not resolved.is_relative_to(skill_root):
                raise BMadResourceSecurityError(f"Resource escapes skill root: {match}")
            if resolved.is_dir():
                for child in resolved.rglob("*"):
                    if child.is_file():
                        child_resolved = child.resolve()
                        if not child_resolved.is_relative_to(skill_root):
                            raise BMadResourceSecurityError(f"Resource escapes skill root: {child}")
                        if child_resolved.suffix.lower() in self.TEXT_EXTENSIONS:
                            results.add(child_resolved)
            elif resolved.is_file():
                if resolved.suffix.lower() not in self.TEXT_EXTENSIONS:
                    raise BMadLoaderError(f"Unsupported text resource type: {resolved.suffix}")
                results.add(resolved)
        if not results:
            raise BMadResourceNotFoundError(
                f"Supporting resource not found: '{reference}' (referenced by {source_file.name})."
            )
        return sorted(results)

    def _read_resource(self, path: Path, skill_root: Path) -> LoadedResource:
        path = path.resolve()
        if not path.is_relative_to(skill_root.resolve()):
            raise BMadResourceSecurityError(f"Resource is outside skill directory: {path}")
        if not path.is_file():
            raise BMadResourceNotFoundError(f"Resource does not exist: {path}")
        suffix = path.suffix.lower()
        if suffix not in self.TEXT_EXTENSIONS:
            raise BMadLoaderError(f"Unsupported text resource type: {suffix}")
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise BMadResourceLimitError(
                f"Resource exceeds individual file limit ({self.max_file_bytes} bytes): {path}"
            )
        content = self._read_text_file(path)
        return LoadedResource(
            name=path.name,
            path=path,
            relative_path=path.relative_to(skill_root).as_posix(),
            content=content,
            resource_type=suffix.lstrip("."),
            size_bytes=size,
        )

    @staticmethod
    def _read_text_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BMadLoaderError(f"Could not decode resource as UTF-8: {path}") from exc
        except OSError as exc:
            raise BMadLoaderError(f"Could not read resource {path}: {exc}") from exc
