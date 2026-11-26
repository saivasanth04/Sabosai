from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

from .bmad_models import PlannedArtifact


class ArtifactStore:
    """Persists generated planning documents inside one configured output directory."""

    def __init__(self, project_root: str | Path, output_dir: str = "docs/planning"):
        self.project_root = Path(project_root).expanduser().resolve()
        self.root = (self.project_root / output_dir).resolve()
        if not self.root.is_relative_to(self.project_root):
            raise PermissionError("Planning output directory must be inside project_root.")
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        posix_path = PurePosixPath(relative_path.replace("\\", "/"))
        windows_path = PureWindowsPath(relative_path)
        if (posix_path.is_absolute() or windows_path.is_absolute()
                or windows_path.drive or ".." in posix_path.parts
                or ".." in windows_path.parts):
            raise PermissionError(f"Artifact path must be relative and remain inside output directory: {relative_path}")
        candidate = (self.root / Path(*posix_path.parts)).resolve()
        if not candidate.is_relative_to(self.root):
            raise PermissionError(f"Artifact path escapes planning output directory: {relative_path}")
        return candidate

    def write(self, artifact: PlannedArtifact) -> Path:
        if not artifact.content.strip():
            raise ValueError(f"Refusing to write empty artifact: {artifact.path}")
        target = self.resolve(artifact.path)
        target.parent.mkdir(parents=True, exist_ok=True)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(artifact.content.rstrip() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
        return target

    def write_many(self, artifacts: Iterable[PlannedArtifact]) -> list[Path]:
        written = []
        seen = set()
        for artifact in artifacts:
            target = self.resolve(artifact.path)
            key = os.path.normcase(str(target))
            if key in seen:
                raise ValueError(f"Duplicate artifact path in one execution: {artifact.path}")
            seen.add(key)
            written.append(self.write(artifact))
        return written

    def read(self, relative_path: str) -> str:
        return self.resolve(relative_path).read_text(encoding="utf-8")

    def list_files(self) -> list[str]:
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.endswith(".tmp")
        )
