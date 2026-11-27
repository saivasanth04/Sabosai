"""Script executor — run BMAD's existing Python / ``uv run`` scripts safely.

BMAD skill text says things like::

    uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root}

The LLM gets a ``run_script`` tool rather than a shell. This module:
  * accepts either a structured call (script path + args) or the literal command line
    copied from the skill (``uv run ...`` / ``python ...`` / ``python3 ...``),
  * checks the script lives inside an allowed root (BMAD clone, ``{project}/_bmad``) via the
    permission manager,
  * runs it with ``uv run`` when uv is on PATH (BMAD scripts carry PEP 723 headers), else
    with the current interpreter — so it works on Windows boxes without uv,
  * never uses ``shell=True``; pins UTF-8 so emoji in agent icons survive cp1252 consoles.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from .errors import ScriptExecutionError

if TYPE_CHECKING:  # pragma: no cover
    from .permissions import PermissionManager

_LAUNCHERS = {"uv", "uv.exe", "python", "python3", "python.exe", "py", "py.exe", "uvx"}
_UV_FLAGS_WITH_VALUE = {"--python", "-p", "--with", "--project", "--directory", "--index-url"}


@dataclass
class ScriptResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    launcher: str = "python"
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def for_llm(self, max_chars: int = 20_000) -> str:
        out = self.stdout if len(self.stdout) <= max_chars else self.stdout[:max_chars] + "\n…[truncated]"
        parts = [f"exit_code: {self.returncode}"]
        if out.strip():
            parts.append("stdout:\n" + out.rstrip())
        if self.stderr.strip():
            parts.append("stderr:\n" + self.stderr.strip()[-4000:])
        return "\n".join(parts)


def split_command(command: str) -> list[str]:
    """Shell-like split that works for both Windows and POSIX paths.

    * double or single quotes group text and may appear mid-token (``topic="Habit app"``)
    * backslashes are literal (``C:\\Projects\\NTG``), except ``\\"`` inside double quotes
    * ``""`` inside double quotes is a literal quote (cmd.exe style)
    """
    tokens: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    touched = False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote == '"':
            if c == "\\" and i + 1 < n and command[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            if c == '"' and i + 1 < n and command[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            if c == '"':
                quote = None
            else:
                cur.append(c)
        elif quote == "'":
            if c == "'":
                quote = None
            else:
                cur.append(c)
        elif c in "\"'":
            quote, touched = c, True
        elif c.isspace():
            if cur or touched:
                tokens.append("".join(cur))
                cur, touched = [], False
        else:
            cur.append(c)
        i += 1
    if quote is not None:
        raise ScriptExecutionError(f"unbalanced quotes in command: {command!r}")
    if cur or touched:
        tokens.append("".join(cur))
    return tokens


class ScriptExecutor:
    def __init__(self, project_root: Path, permissions: "PermissionManager",
                 timeout_s: float = 300.0, prefer_uv: bool = True) -> None:
        self.project_root = Path(project_root)
        self.permissions = permissions
        self.timeout_s = timeout_s
        self.uv = shutil.which("uv") if prefer_uv else None

    # ------------------------------------------------------------------ parse
    def parse_command(self, command: str) -> tuple[Path, list[str]]:
        """Turn ``uv run path/x.py --a b`` / ``python x.py`` into (script, args)."""
        tokens = split_command(command)
        if not tokens:
            raise ScriptExecutionError("empty command")
        head = Path(tokens[0]).name.lower()
        if head not in _LAUNCHERS and not tokens[0].endswith(".py"):
            raise ScriptExecutionError(
                f"only `uv run <script.py>` or `python <script.py>` commands are supported, got {tokens[0]!r}")
        i = 0
        if head in {"uv", "uv.exe"}:
            i = 1
            if i < len(tokens) and tokens[i] == "run":
                i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in _UV_FLAGS_WITH_VALUE else 1
        elif head in _LAUNCHERS:
            i = 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
        if i >= len(tokens):
            raise ScriptExecutionError(f"no script found in command: {command!r}")
        script = Path(tokens[i])
        if not script.is_absolute():
            script = (self.project_root / script).resolve()
        return script, tokens[i + 1 :]

    # -------------------------------------------------------------------- run
    def run_command(self, command: str, *, cwd: Path | None = None, stdin: str | None = None) -> ScriptResult:
        script, args = self.parse_command(command)
        return self.run(script, args, cwd=cwd, stdin=stdin)

    def run(self, script: str | Path, args: Sequence[str] = (), *, cwd: Path | None = None,
            stdin: str | None = None, timeout_s: float | None = None) -> ScriptResult:
        script_path = Path(script).expanduser().resolve()
        if script_path.suffix != ".py":
            raise ScriptExecutionError(f"only Python scripts can run: {script_path}")
        if not script_path.is_file():
            raise ScriptExecutionError(f"script not found: {script_path}")
        self.permissions.check_script(script_path, list(args))

        if self.uv:
            cmd = [self.uv, "run", "--no-project", "--quiet", str(script_path), *map(str, args)]
            launcher = "uv"
        else:
            cmd = [sys.executable, str(script_path), *map(str, args)]
            launcher = "python"
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd or self.project_root), input=stdin, capture_output=True,
                text=True, encoding="utf-8", errors="replace", env=env,
                timeout=timeout_s or self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScriptExecutionError(f"script timed out after {exc.timeout}s: {script_path.name}") from exc
        except FileNotFoundError as exc:
            if launcher == "uv":          # uv vanished / broken → retry with python
                self.uv = None
                return self.run(script_path, args, cwd=cwd, stdin=stdin, timeout_s=timeout_s)
            raise ScriptExecutionError(str(exc)) from exc
        result = ScriptResult(cmd, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start, launcher)
        if launcher == "uv" and proc.returncode != 0 and "error:" in proc.stderr[:200].lower() and "uv" in proc.stderr[:200].lower():
            self.uv = None              # uv-level failure (offline index etc.) → fall back once
            fallback = self.run(script_path, args, cwd=cwd, stdin=stdin, timeout_s=timeout_s)
            fallback.notes.append("uv failed; re-ran with the current Python interpreter")
            return fallback
        return result
