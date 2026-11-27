"""Configuration resolver — interface with BMAD's own config scripts.

Two jobs:

1. **Project bootstrap** (what ``npx bmad-method install`` normally does for an IDE):
   lays down ``{project}/_bmad/`` so unmodified BMAD skills work:
     * ``_bmad/scripts/*.py``        copied from ``{bmad_root}/src/scripts`` (kept in sync)
     * ``_bmad/config.toml``         [core], [modules.bmm], [agents.*]  (team scope)
     * ``_bmad/config.user.toml``    user-scoped answers
     * ``_bmad/_config/bmad-help.csv`` + ``skill-manifest.csv``  (bmad-help catalog)
     * ``_bmad/custom/``             your overrides (never touched)
   An existing installer-managed ``_bmad`` (from the real npm installer) is respected.

2. **Resolution**: run ``resolve_config.py`` / ``resolve_customization.py`` through the
   script executor (exactly what the skills ask for) and fall back to importing BMAD's
   ``config_utils`` in-process when the subprocess route fails. Results are cached until a
   layer file changes.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import shutil
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import BMadConfigError, ScriptExecutionError
from .settings import BMadInstallDefaults

if TYPE_CHECKING:  # pragma: no cover
    from .registry import BMadRegistry
    from .script_executor import ScriptExecutor

ADAPTER_MARKER = "# Managed by bmad_adapter"
_HELP_HEADER = ["module", "skill", "display-name", "menu-code", "description", "action", "args", "phase",
                "preceded-by", "followed-by", "required", "output-location", "outputs"]


class ConfigResolver:
    def __init__(self, bmad_root: Path, project_root: Path, registry: "BMadRegistry",
                 scripts: "ScriptExecutor", defaults: BMadInstallDefaults) -> None:
        self.bmad_root = Path(bmad_root)
        self.project_root = Path(project_root)
        self.registry = registry
        self.scripts = scripts
        self.defaults = defaults
        self.bmad_dir = self.project_root / "_bmad"
        self._cache: dict[tuple, tuple[str, Any]] = {}
        self._config_utils = None

    # ================================================================ bootstrap
    @property
    def scripts_dir(self) -> Path:
        return self.bmad_dir / "scripts"

    def ensure_installed(self, force: bool = False) -> dict[str, Any]:
        """Idempotently create/refresh ``{project}/_bmad``. Returns a report."""
        report: dict[str, Any] = {"project_root": str(self.project_root), "actions": []}
        self.bmad_dir.mkdir(parents=True, exist_ok=True)
        (self.bmad_dir / "custom").mkdir(exist_ok=True)

        # 1. shared scripts (always kept in sync with the clone)
        src = self.bmad_root / "src" / "scripts"
        self.scripts_dir.mkdir(exist_ok=True)
        for script in sorted(src.glob("*.py")):
            dest = self.scripts_dir / script.name
            if force or not dest.exists() or _sha(dest) != _sha(script):
                shutil.copy2(script, dest)
                report["actions"].append(f"synced scripts/{script.name}")

        # 2. central config (do not clobber a real installer's files)
        team = self.bmad_dir / "config.toml"
        user = self.bmad_dir / "config.user.toml"
        team_text, user_text = self._render_central_config()
        for path, text in ((team, team_text), (user, user_text)):
            if path.exists() and ADAPTER_MARKER not in path.read_text(encoding="utf-8", errors="ignore") and not force:
                report["actions"].append(f"kept existing {path.name} (installer-managed)")
                continue
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
                report["actions"].append(f"wrote {path.name}")

        # 3. help catalog + skill manifest used by bmad-help
        cfg_dir = self.bmad_dir / "_config"
        cfg_dir.mkdir(exist_ok=True)
        help_csv = self._render_help_csv()
        if not (cfg_dir / "bmad-help.csv").exists() or (cfg_dir / "bmad-help.csv").read_text(encoding="utf-8") != help_csv:
            (cfg_dir / "bmad-help.csv").write_text(help_csv, encoding="utf-8")
            report["actions"].append("wrote _config/bmad-help.csv")
        (cfg_dir / "skill-manifest.csv").write_text(self._render_skill_manifest(), encoding="utf-8")

        # 4. output folder (the one directory BMAD creates at install time)
        out = self.project_root / self.defaults.output_folder
        out.mkdir(parents=True, exist_ok=True)

        # 5. gitignore the runtime + personal files
        gi = self.bmad_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("config.user.toml\ncustom/*.user.toml\nrender/\nruntime/\n", encoding="utf-8")
        self._cache.clear()
        return report

    def _render_central_config(self) -> tuple[str, str]:
        d = self.defaults
        project_name = d.project_name or self.project_root.name
        out = d.output_folder.strip("/\\")

        def proj(value: str) -> str:
            value = value.replace("{output_folder}", out)
            return value if value.startswith("{project-root}") else "{project-root}/" + value.strip("/\\")

        team = [ADAPTER_MARKER + " (equivalent of the BMAD installer's config.toml).",
                "# Pin overrides in _bmad/custom/config.toml — that file is never rewritten.", "",
                "[core]",
                f"user_name = {_q(d.user_name)}",
                f"project_name = {_q(project_name)}",
                f"communication_language = {_q(d.communication_language)}",
                f"document_output_language = {_q(d.document_output_language)}",
                f"output_folder = {_q(proj(out))}", "",
                "[modules.bmm]",
                f"user_skill_level = {_q(d.user_skill_level)}",
                f"planning_artifacts = {_q(proj(d.planning_artifacts))}",
                f"implementation_artifacts = {_q(proj(d.implementation_artifacts))}",
                f"project_knowledge = {_q(proj(d.project_knowledge))}", ""]
        for agent in self.registry.list_agents():
            team += [f"[agents.{agent.agent_id}]", f"module = {_q(agent.module_code or 'bmm')}",
                     f"team = {_q(agent.team or 'bmm')}", f"name = {_q(agent.name)}",
                     f"title = {_q(agent.title)}", f"icon = {_q(agent.icon)}",
                     f"description = {_q(agent.description)}", ""]
        user = [ADAPTER_MARKER + " — personal answers.", "", "[core]",
                f"user_name = {_q(d.user_name)}",
                f"communication_language = {_q(d.communication_language)}", "",
                "[modules.bmm]", f"user_skill_level = {_q(d.user_skill_level)}", ""]
        return "\n".join(team), "\n".join(user)

    def _render_help_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(_HELP_HEADER)
        for path in sorted((self.bmad_root / "src").rglob("module-help.csv")):
            if any(p in {"test", "tests", "node_modules"} for p in path.parts):
                continue
            rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
            for row in rows[1:]:
                if row:
                    writer.writerow(row)
        return buf.getvalue()

    def _render_skill_manifest(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["canonicalId", "name", "description", "module", "path"])
        for s in self.registry.list_skills():
            w.writerow([s.skill_id, s.name, s.description, s.module_code or "", str(s.skill_dir)])
        return buf.getvalue()

    # =============================================================== resolution
    def central_config(self) -> dict[str, Any]:
        """Merged four-layer config (config.toml → user → custom → custom.user)."""
        layers = [self.bmad_dir / n for n in ("config.toml", "config.user.toml",
                                              "custom/config.toml", "custom/config.user.toml")]
        key = ("central",)
        stamp = _stamp(layers)
        if key in self._cache and self._cache[key][0] == stamp:
            return self._cache[key][1]
        if not layers[0].exists():
            self.ensure_installed()
        data = self._run_json(self.scripts_dir / "resolve_config.py", ["--project-root", str(self.project_root)])
        if data is None:
            data = self._utils().load_central_config(self.project_root)
        self._cache[key] = (_stamp(layers), data)
        return data

    def customization(self, skill_dir: Path, key: str | None = None) -> dict[str, Any]:
        """Three-layer skill customization (customize.toml → team → user)."""
        skill_dir = Path(skill_dir)
        if not (skill_dir / "customize.toml").exists():
            return {}
        layers = [skill_dir / "customize.toml", self.bmad_dir / "custom" / f"{skill_dir.name}.toml",
                  self.bmad_dir / "custom" / f"{skill_dir.name}.user.toml"]
        ck = ("custom", str(skill_dir), key)
        stamp = _stamp(layers)
        if ck in self._cache and self._cache[ck][0] == stamp:
            return self._cache[ck][1]
        args = ["--skill", str(skill_dir), "--project-root", str(self.project_root)]
        if key:
            args += ["--key", key]
        data = self._run_json(self.scripts_dir / "resolve_customization.py", args)
        if data is None:
            merged = self._utils().load_customization(self.project_root, skill_dir)
            data = {key: merged[key]} if key and key in merged else ({} if key else merged)
        self._cache[ck] = (stamp, data)
        return data

    def variables(self) -> dict[str, str]:
        """Flat variables BMAD skills reference ({user_name}, {planning_artifacts}, {date}, …)."""
        cfg = self.central_config()
        flat: dict[str, str] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, (dict, list)):
                        if k != "agents":
                            walk(v)
                    elif k not in flat:
                        flat[k] = str(v)
        walk(cfg.get("core", {}))
        walk(cfg.get("modules", {}))
        walk({k: v for k, v in cfg.items() if k not in {"core", "modules", "agents"}})
        root = str(self.project_root)
        flat = {k: v.replace("{project-root}", root) for k, v in flat.items()}
        flat.setdefault("user_name", self.defaults.user_name)
        flat.setdefault("communication_language", self.defaults.communication_language)
        flat.setdefault("document_output_language", self.defaults.document_output_language)
        flat.setdefault("project_name", self.defaults.project_name or self.project_root.name)
        flat["project-root"] = root
        flat["date"] = date.today().isoformat()
        return flat

    # ---------------------------------------------------------------- helpers
    def _run_json(self, script: Path, args: list[str]) -> Any | None:
        if not script.exists():
            return None
        try:
            res = self.scripts.run(script, args, timeout_s=60)
        except ScriptExecutionError:
            return None
        if not res.ok:
            if "error:" in res.stderr:
                raise BMadConfigError(f"{script.name} failed: {res.stderr.strip()}")
            return None
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError:
            return None

    def _utils(self):
        if self._config_utils is None:
            src = self.scripts_dir / "config_utils.py"
            if not src.exists():
                src = self.bmad_root / "src" / "scripts" / "config_utils.py"
            spec = importlib.util.spec_from_file_location("bmad_config_utils", src)
            if spec is None or spec.loader is None:  # pragma: no cover
                raise BMadConfigError(f"cannot import {src}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._config_utils = module
        return self._config_utils


def _q(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stamp(paths: list[Path]) -> tuple:
    return tuple((str(p), p.stat().st_mtime_ns if p.exists() else 0) for p in paths)
