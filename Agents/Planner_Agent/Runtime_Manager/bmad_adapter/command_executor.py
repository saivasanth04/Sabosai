"""Command executor — a scoped developer shell for build / test / package commands.

BMAD's implementation skills (bmad-build, bmad-code-review, bmad-qa-generate-e2e-tests)
expect the agent to install dependencies, build, run tests and inspect git — things the
BMAD *script* executor deliberately cannot do. This executor allows exactly that, safely:

* only allow-listed executables (npm, npx, node, pnpm, yarn, git, python, pytest, tsc …)
* one command per call — no shell, no ``&&`` / ``|`` / ``>`` chaining, no ``shell=True``
* working directory must be inside the project root
* dangerous sub-commands refused (``git push``, ``git reset --hard``, ``npm publish`` …)
* long-running servers (``npm run dev``, ``npm start``, ``vite``) only with ``background``;
  background processes are tracked, logged to ``_bmad/runtime/processes`` and stoppable
* Windows ``.cmd`` shims (npm.cmd, npx.cmd) resolved via PATH; arguments containing cmd.exe
  metacharacters are refused for them (BatBadBut-style injection)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import PermissionDeniedError, ScriptExecutionError
from .script_executor import split_command

if TYPE_CHECKING:  # pragma: no cover
    from .permissions import PermissionManager

DEFAULT_ALLOWED = ["npm", "npx", "node", "pnpm", "yarn", "bun", "git", "python", "python3", "py", "pip", "uv",
                   "pytest", "tsc", "eslint", "prettier", "vite", "vitest", "jest", "go", "cargo", "dotnet",
                   "mvn", "gradle", "java", "javac", "deno"]
_DENY = [r"^git push\b", r"^git reset --hard\b", r"^git clean\b", r"^git remote (add|set-url|remove)\b",
         r"^git config --global\b", r"^(npm|pnpm|yarn|bun) publish\b", r"^npm (adduser|login|logout|token)\b",
         r"^pip uninstall\b", r"^npm (un|uninstall) -g\b", r"\b--global\b|\s-g(\s|$)"]
_LONG_RUNNING = [r"^(npm|pnpm|yarn|bun) (run )?(dev|start|serve|watch|preview)\b", r"^vite(\s|$)(?!build)",
                 r"^node .*--watch", r"^npx (nodemon|vite)(\s|$)(?!build)", r"^nodemon\b"]
_CHAIN = {"&&", "||", "|", ";", ">", ">>", "<", "&", "2>", "2>&1"}
_CMD_META = set('&|<>^%"\n\r')


@dataclass
class CommandResult:
    command: list[str]
    cwd: str
    returncode: int
    output: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def for_llm(self, max_chars: int = 15_000) -> str:
        out = self.output
        if len(out) > max_chars:
            out = "…[earlier output truncated]\n" + out[-max_chars:]
        status = "TIMED OUT" if self.timed_out else f"exit_code: {self.returncode}"
        return f"$ {' '.join(self.command)}   (cwd: {self.cwd})\n{status}  ({self.duration_s:.1f}s)\n{out.rstrip()}"


@dataclass
class BackgroundProcess:
    proc_id: str
    command: list[str]
    cwd: str
    pid: int
    log: str
    started: float = field(default_factory=time.time)


class CommandExecutor:
    def __init__(self, project_root: Path, permissions: "PermissionManager", *,
                 allowed: list[str] | None = None, timeout_s: float = 600.0) -> None:
        self.project_root = Path(project_root).resolve()
        self.permissions = permissions
        self.allowed = {a.lower() for a in (allowed or DEFAULT_ALLOWED)}
        self.timeout_s = timeout_s
        self.proc_dir = self.project_root / "_bmad" / "runtime" / "processes"
        self._bg: dict[str, tuple[BackgroundProcess, subprocess.Popen]] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- policy
    def prepare(self, command: str | list[str], cwd: str | None = None, background: bool = False) -> tuple[list[str], Path]:
        tokens = split_command(command) if isinstance(command, str) else [str(t) for t in command]
        if not tokens:
            raise ScriptExecutionError("empty command")
        if any(t in _CHAIN for t in tokens):
            raise ScriptExecutionError("one command per call — no &&, ||, |, ;, or redirects. Run them separately.")
        exe_name = Path(tokens[0]).name.lower().removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
        if exe_name not in self.allowed:
            raise PermissionDeniedError(f"`{exe_name}` is not an allowed command. Allowed: {', '.join(sorted(self.allowed))}")
        line = " ".join([exe_name, *tokens[1:]])
        for pat in _DENY:
            if re.search(pat, line):
                raise PermissionDeniedError(f"refused for safety: `{line}` (matches {pat})")
        if not background and any(re.search(p, line) for p in _LONG_RUNNING):
            raise ScriptExecutionError(f"`{line}` starts a long-running server. Use background=true (then "
                                       "stop_process) or run a one-shot check such as `npm run build` instead.")
        workdir = self.permissions.check_write(cwd or self.project_root)
        if not workdir.is_dir():
            raise ScriptExecutionError(f"working directory does not exist: {workdir}")
        resolved = shutil.which(tokens[0], path=os.environ.get("PATH"))
        if resolved is None:
            raise ScriptExecutionError(f"`{tokens[0]}` is not installed or not on PATH")
        if resolved.lower().endswith((".cmd", ".bat")) and any(set(t) & _CMD_META for t in tokens[1:]):
            raise PermissionDeniedError("arguments with & | < > ^ % or quotes are refused for .cmd tools on Windows")
        self.permissions.check_command(line, workdir)
        return [resolved, *tokens[1:]], workdir

    # -------------------------------------------------------------------- run
    def run(self, command: str | list[str], cwd: str | None = None, timeout_s: float | None = None) -> CommandResult:
        argv, workdir = self.prepare(command, cwd)
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=str(workdir), capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", env=_env(), stdin=subprocess.DEVNULL,
                                  timeout=timeout_s or self.timeout_s)
            out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else "")
            return CommandResult(_display(argv), str(workdir), proc.returncode, out, time.monotonic() - start)
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return CommandResult(_display(argv), str(workdir), -1, out, time.monotonic() - start, timed_out=True)

    def start_background(self, command: str | list[str], cwd: str | None = None) -> BackgroundProcess:
        argv, workdir = self.prepare(command, cwd, background=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        pid_ = f"proc-{uuid.uuid4().hex[:6]}"
        log_path = self.proc_dir / f"{pid_}.log"
        log = open(log_path, "w", encoding="utf-8", errors="replace")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(argv, cwd=str(workdir), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=_env(), creationflags=flags, start_new_session=os.name != "nt")
        bp = BackgroundProcess(pid_, _display(argv), str(workdir), proc.pid, str(log_path))
        with self._lock:
            self._bg[pid_] = (bp, proc)
        return bp

    def status(self, proc_id: str, tail: int = 4000) -> str:
        bp, proc = self._get(proc_id)
        code = proc.poll()
        text = Path(bp.log).read_text(encoding="utf-8", errors="replace")[-tail:]
        state = "running" if code is None else f"exited ({code})"
        return f"{proc_id} {state} — $ {' '.join(bp.command)}\n{text}"

    def stop(self, proc_id: str) -> str:
        bp, proc = self._get(proc_id)
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        return f"stopped {proc_id}"

    def stop_all(self) -> None:
        for pid_ in list(self._bg):
            try:
                self.stop(pid_)
            except Exception:  # noqa: BLE001
                pass

    def smoke_start(self, command: str | list[str], cwd: str | None = None, seconds: float = 6.0) -> CommandResult:
        """Start a server, wait, report whether it crashed (e.g. missing module) — then stop it."""
        bp = self.start_background(command, cwd)
        _, proc = self._get(bp.proc_id)
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds and proc.poll() is None:
            time.sleep(0.3)
        crashed = proc.poll() is not None and proc.returncode != 0
        exited = proc.poll()
        self.stop(bp.proc_id)
        out = Path(bp.log).read_text(encoding="utf-8", errors="replace")[-6000:]
        rc = (exited or 1) if crashed else 0
        note = "crashed during start-up" if crashed else f"still running after {seconds:.0f}s (started OK)"
        return CommandResult(bp.command, bp.cwd, rc, f"[{note}]\n{out}", time.monotonic() - t0)

    def _get(self, proc_id: str):
        with self._lock:
            if proc_id not in self._bg:
                raise ScriptExecutionError(f"no background process {proc_id}")
            return self._bg[proc_id]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"CI": "true", "FORCE_COLOR": "0", "NO_COLOR": "1", "npm_config_yes": "true",
                "npm_config_fund": "false", "npm_config_audit": "false", "PYTHONUTF8": "1",
                "GIT_TERMINAL_PROMPT": "0"})
    return env


def _display(argv: list[str]) -> list[str]:
    return [Path(argv[0]).name, *argv[1:]]
