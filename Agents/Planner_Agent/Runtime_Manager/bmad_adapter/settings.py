"""Adapter settings: one TOML file (bmad_adapter.toml) plus environment overrides.

Resolution order (later wins):
  1. dataclass defaults
  2. bmad_adapter.toml  (path from BMAD_ADAPTER_CONFIG, else next to this package's parent)
  3. environment variables (GOMODEL_BASE_URL, GOMODEL_API_KEY, GOMODEL_MODEL, BMAD_METHOD_ROOT,
     BMAD_PROJECT_ROOT, ...)
  4. keyword overrides passed to ``AdapterSettings.load(**overrides)``
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
RUNTIME_MANAGER_DIR = PACKAGE_DIR.parent
PLANNER_AGENT_DIR = RUNTIME_MANAGER_DIR.parent


@dataclass
class GoModelSettings:
    base_url: str = "http://localhost:8080"
    api_key_env: str = "GOMODEL_API_KEY"
    model: str = "gpt-4o"
    subagent_model: str | None = None          # defaults to ``model``
    temperature: float | None = 0.3
    max_output_tokens: int | None = 8192
    request_timeout_s: float = 300.0
    max_retries: int = 3
    # MCP gateway (GoModel aggregates config.yaml `mcp.servers` behind /mcp)
    mcp_enabled: bool = True
    mcp_path: str = "/mcp"                     # or "/mcp/<virtual-server>"
    mcp_servers: list[str] = field(default_factory=list)  # X-MCP-Servers subset; [] = all
    mcp_tool_discovery: str | None = None      # None = gateway default, or "off" / "search"
    mcp_timeout_s: float = 180.0

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or os.environ.get("GOMODEL_MASTER_KEY")


@dataclass
class ExecutionSettings:
    max_turns_per_message: int = 60            # LLM round-trips before the engine yields
    max_tool_calls_per_turn: int = 16
    context_token_budget: int = 110_000        # rough (chars/4) budget for the message list
    compaction_keep_recent: int = 14           # messages kept verbatim when compacting
    tool_result_max_chars: int = 24_000        # truncate huge tool outputs before they hit context
    subagent_max_turns: int = 30
    subagent_parallelism: int = 4
    headless: bool = False                     # default mode for runtime.run_skill()


@dataclass
class PermissionSettings:
    mode: str = "auto"                         # auto | ask | readonly
    extra_read_roots: list[str] = field(default_factory=list)
    extra_write_roots: list[str] = field(default_factory=list)
    allow_tools: list[str] = field(default_factory=lambda: ["*"])   # fnmatch patterns
    deny_tools: list[str] = field(default_factory=list)
    ask_tools: list[str] = field(default_factory=list)             # always confirm these
    script_timeout_s: float = 300.0
    allow_arbitrary_scripts: bool = False      # only scripts inside BMAD / _bmad roots by default
    protect_bmad_source: bool = True           # never write into the BMAD clone
    allow_commands: bool = True                # run_command: npm/node/git/pytest… inside the project
    allowed_commands: list[str] = field(default_factory=list)   # [] = built-in developer allow-list
    command_timeout_s: float = 600.0


@dataclass
class FilesystemSettings:
    backend: str = "auto"                      # auto | mcp | local  (agent file tools → GoModel filesystem MCP)
    mcp_server: str = "filesystem"             # GoModel server slug in config.yaml
    expose_raw_mcp_tools: bool = False         # hide filesystem_* from agents (they bypass permissions/tracking)
    fallback_to_local: bool = True             # auto mode: tool not whitelisted / path outside roots → local


@dataclass
class LoaderSettings:
    max_resource_files: int = 400
    max_file_bytes: int = 512_000
    max_total_bytes: int = 4_000_000


@dataclass
class BMadInstallDefaults:
    """Answers the BMAD installer would normally ask (written to _bmad/config*.toml)."""

    user_name: str = "Planner"
    project_name: str | None = None            # default: project folder name
    communication_language: str = "English"
    document_output_language: str = "English"
    output_folder: str = "_bmad-output"
    user_skill_level: str = "intermediate"
    planning_artifacts: str = "{output_folder}/planning-artifacts"
    implementation_artifacts: str = "{output_folder}/implementation-artifacts"
    project_knowledge: str = "docs"


@dataclass
class AdapterSettings:
    bmad_root: str | None = None               # BMAD-METHOD clone (auto-detected when None)
    project_root: str | None = None            # target project the planner works on
    session_dir: str | None = None             # default: {project_root}/_bmad/runtime/sessions
    extra_skill_roots: list[str] = field(default_factory=list)
    gomodel: GoModelSettings = field(default_factory=GoModelSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    loader: LoaderSettings = field(default_factory=LoaderSettings)
    filesystem: FilesystemSettings = field(default_factory=FilesystemSettings)
    install: BMadInstallDefaults = field(default_factory=BMadInstallDefaults)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, config_path: str | os.PathLike | None = None, **overrides: Any) -> "AdapterSettings":
        load_dotenv()
        settings = cls()
        path = Path(config_path) if config_path else _default_config_path()
        if path and path.is_file():
            with path.open("rb") as fh:
                settings = _apply(settings, tomllib.load(fh))
        settings = _apply_env(settings)
        if overrides:
            settings = _apply(settings, overrides)
        return settings

    # ------------------------------------------------------------- derived paths
    def resolved_bmad_root(self) -> Path:
        if self.bmad_root:
            root = Path(self.bmad_root).expanduser().resolve()
            if not root.is_dir():
                raise FileNotFoundError(f"Configured BMAD root does not exist: {root}")
            return root
        candidates = [
            PLANNER_AGENT_DIR / "Plugins" / "BMAD-METHOD-6.12.4" / "BMAD-METHOD-6.12.4",
            PLANNER_AGENT_DIR / "Plugins" / "BMAD-METHOD-6.12.4",
            PLANNER_AGENT_DIR / "Plugins" / "BMAD-METHOD",
            PLANNER_AGENT_DIR / "framework" / "BMAD-METHOD",
        ]
        plugins = PLANNER_AGENT_DIR / "Plugins"
        if plugins.is_dir():
            candidates += sorted(p for p in plugins.glob("BMAD-METHOD*/**/") if (p / "src" / "core-skills").is_dir())
        for candidate in candidates:
            if (candidate / "src" / "core-skills").is_dir():
                return candidate.resolve()
        checked = "\n  ".join(str(c) for c in candidates[:4])
        raise FileNotFoundError(
            "Could not locate the BMAD-METHOD clone. Checked:\n  " + checked
            + "\nSet bmad_root in bmad_adapter.toml or BMAD_METHOD_ROOT."
        )

    def resolved_project_root(self) -> Path:
        root = Path(self.project_root).expanduser() if self.project_root else PLANNER_AGENT_DIR / "Workspace"
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def resolved_session_dir(self, project_root: Path) -> Path:
        path = Path(self.session_dir).expanduser() if self.session_dir else project_root / "_bmad" / "runtime" / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()


# ---------------------------------------------------------------------- helpers
NTG_ROOT = PLANNER_AGENT_DIR.parent.parent
_DOTENV_LOADED: set[str] = set()


def load_dotenv(paths: list[Path] | None = None) -> list[str]:
    """Read KEY=VALUE files (the same .env GoModel uses) without overriding real env vars.

    Default search: BMAD_ADAPTER_DOTENV, Runtime_Manager/.env, Planner_Agent/.env, C:\\Projects\\NTG\\.env.
    Returns the files that were read. Values are never logged.
    """
    if paths is None:
        env_path = os.environ.get("BMAD_ADAPTER_DOTENV")
        paths = [Path(env_path)] if env_path else [RUNTIME_MANAGER_DIR / ".env", PLANNER_AGENT_DIR / ".env",
                                                   NTG_ROOT / ".env"]
    read = []
    for path in paths:
        key = str(path)
        if key in _DOTENV_LOADED or not path.is_file():
            continue
        _DOTENV_LOADED.add(key)
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
        read.append(key)
    return read


def _default_config_path() -> Path | None:
    env = os.environ.get("BMAD_ADAPTER_CONFIG")
    if env:
        return Path(env)
    for candidate in (RUNTIME_MANAGER_DIR / "bmad_adapter.toml", PACKAGE_DIR / "bmad_adapter.toml"):
        if candidate.is_file():
            return candidate
    return None


def _apply(obj: Any, data: dict[str, Any]) -> Any:
    """Return a copy of dataclass ``obj`` with nested dict ``data`` applied."""
    if not data:
        return obj
    names = {f.name: f for f in fields(obj)}
    changes: dict[str, Any] = {}
    for key, value in data.items():
        key = key.replace("-", "_")
        if key not in names:
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            changes[key] = _apply(current, value)
        else:
            changes[key] = value
    return replace(obj, **changes)


def _apply_env(s: AdapterSettings) -> AdapterSettings:
    env = os.environ
    top: dict[str, Any] = {}
    if env.get("BMAD_METHOD_ROOT"):
        top["bmad_root"] = env["BMAD_METHOD_ROOT"]
    if env.get("BMAD_PROJECT_ROOT"):
        top["project_root"] = env["BMAD_PROJECT_ROOT"]
    gm: dict[str, Any] = {}
    if env.get("GOMODEL_BASE_URL"):
        gm["base_url"] = env["GOMODEL_BASE_URL"]
    if env.get("GOMODEL_MODEL"):
        gm["model"] = env["GOMODEL_MODEL"]
    if env.get("GOMODEL_SUBAGENT_MODEL"):
        gm["subagent_model"] = env["GOMODEL_SUBAGENT_MODEL"]
    if env.get("GOMODEL_MCP_PATH"):
        gm["mcp_path"] = env["GOMODEL_MCP_PATH"]
    if env.get("GOMODEL_MCP_ENABLED"):
        gm["mcp_enabled"] = env["GOMODEL_MCP_ENABLED"].lower() in {"1", "true", "yes"}
    if gm:
        top["gomodel"] = gm
    return _apply(s, top)
