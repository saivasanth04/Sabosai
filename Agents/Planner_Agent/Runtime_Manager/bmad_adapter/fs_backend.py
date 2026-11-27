"""Filesystem backend — agent file I/O through GoModel's filesystem MCP server.

The adapter's file tools (read_file, list_dir, search_files, write_file, append_file, edit_file,
create_directory, move_file, copy_file, directory_tree, file_info, read_multiple_files) keep their
names and their adapter-side guarantees — permission checks, artifact + step tracking, BMAD path
token binding — but the actual I/O is done by ``@modelcontextprotocol/server-filesystem`` behind
GoModel (tools ``filesystem_read_text_file``, ``filesystem_write_file``, …).

Backends (``[filesystem].backend``):
  * ``mcp``   every operation through GoModel; fails if the tool is missing/not whitelisted
  * ``auto``  (default) through GoModel when the tool is exposed and the path is inside the
              server's allowed directories, otherwise local — so a read-only whitelist still works
  * ``local`` direct Python I/O (no GoModel needed)

Runtime-internal files (sessions, artifact index, BMAD config bootstrap, registry scans) stay
local: they are infrastructure, not agent actions.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from .errors import MCPError, ToolError

if TYPE_CHECKING:  # pragma: no cover
    from .mcp_client import GoModelMCPClient

# operation → upstream tool name (without the "<slug>_" prefix)
MCP_OPS = {
    "read": "read_text_file", "read_many": "read_multiple_files", "list": "list_directory",
    "list_sizes": "list_directory_with_sizes", "tree": "directory_tree", "search": "search_files",
    "info": "get_file_info", "write": "write_file", "append": "append_file", "edit": "edit_file",
    "mkdir": "create_directory", "move": "move_file", "copy": "copy_file", "roots": "list_allowed_directories",
}
WRITE_OPS = {"write", "append", "edit", "mkdir", "move", "copy"}


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int | None = None


@dataclass
class FsStats:
    mcp_calls: int = 0
    local_calls: int = 0
    fallbacks: list[str] = field(default_factory=list)


class LocalFs:
    name = "local"

    def read(self, p: Path, head: int | None = None, tail: int | None = None) -> str:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        if head is not None or tail is not None:
            lines = text.splitlines()
            lines = lines[:head] if head is not None else lines[-tail:] if tail else []
            text = "\n".join(lines)
        return text

    def list(self, p: Path) -> list[Entry]:
        return [Entry(c.name, c.is_dir(), c.stat().st_size if c.is_file() else None) for c in sorted(p.iterdir())]

    def tree(self, p: Path, depth: int | None, exclude: list[str]) -> list[dict[str, Any]]:
        def walk(d: Path, level: int):
            out = []
            for c in sorted(d.iterdir()):
                rel = c.relative_to(p).as_posix()
                if any(fnmatch.fnmatch(c.name, pat) or fnmatch.fnmatch(rel, pat) for pat in exclude):
                    continue
                item: dict[str, Any] = {"name": c.name, "type": "directory" if c.is_dir() else "file"}
                if c.is_dir() and (depth is None or level < depth):
                    item["children"] = walk(c, level + 1)
                out.append(item)
            return out
        return walk(p, 1)

    def search(self, p: Path, pattern: str, exclude: list[str]) -> list[str]:
        hits = []
        for c in p.glob(pattern):
            rel = c.relative_to(p).as_posix()
            if not any(fnmatch.fnmatch(rel, e) or e in rel.split("/") for e in exclude):
                hits.append(str(c))
        return sorted(hits)

    def info(self, p: Path) -> str:
        st = p.stat()
        return f"size: {st.st_size}\nmodified: {st.st_mtime}\nisDirectory: {p.is_dir()}\nisFile: {p.is_file()}"

    def write(self, p: Path, content: str) -> str:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)
        return f"wrote {p}"

    def append(self, p: Path, content: str) -> str:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(content)
        return f"appended to {p}"

    def edit(self, p: Path, edits: list[dict[str, str]], dry_run: bool = False) -> str:
        text = original = p.read_text(encoding="utf-8")
        for e in edits:
            old, new = e["oldText"], e["newText"]
            if old not in text:
                raise ToolError(f"oldText not found in {p.name} — re-read the file and copy the exact text:\n{old[:300]}")
            text = text.replace(old, new, 1)
        if not dry_run:
            p.write_text(text, encoding="utf-8")
        return _mini_diff(original, text, p.name)

    def mkdir(self, p: Path) -> str:
        p.mkdir(parents=True, exist_ok=True)
        return f"created {p}"

    def move(self, src: Path, dst: Path) -> str:
        if dst.exists():
            raise ToolError(f"destination exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return f"moved {src} → {dst}"

    def copy(self, src: Path, dst: Path) -> str:
        import shutil
        if dst.exists():
            raise ToolError(f"destination exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return f"copied {src} → {dst}"


class McpFs:
    """Calls GoModel-namespaced filesystem tools (``<slug>_<tool>``)."""

    name = "mcp"

    def __init__(self, client: "GoModelMCPClient", slug: str, exposed: set[str]) -> None:
        self.client = client
        self.slug = slug
        self.exposed = exposed            # upstream names GoModel exposes for this server
        self.roots: list[str] = []

    def has(self, op: str) -> bool:
        return MCP_OPS[op] in self.exposed

    def call(self, op: str, args: dict[str, Any]) -> str:
        name = f"{self.slug}_{MCP_OPS[op]}"
        try:
            text, is_error = self.client.call_tool(name, args)
        except MCPError as exc:
            raise ToolError(f"{name}: {exc}") from exc
        if is_error:
            raise ToolError(f"{name}: {text.strip()[:1500]}")
        return text

    def load_roots(self) -> list[str]:
        if self.has("roots"):
            try:
                text = self.call("roots", {})
                block = text.split("\n\n")[0]
                self.roots = [ln.strip() for ln in block.splitlines()[1:] if ln.strip()]
            except ToolError:
                self.roots = []
        return self.roots

    def covers(self, p: Path) -> bool:
        if not self.roots:
            return True                   # unknown → let the server decide
        return any(_within(str(p), r) for r in self.roots)

    # -------------------------------------------------------------- operations
    def read(self, p: Path, head: int | None = None, tail: int | None = None) -> str:
        args: dict[str, Any] = {"path": str(p)}
        if head is not None:
            args["head"] = int(head)
        if tail is not None:
            args["tail"] = int(tail)
        return self.call("read", args)

    def read_many(self, paths: list[Path]) -> str:
        return self.call("read_many", {"paths": [str(p) for p in paths]})

    def list(self, p: Path) -> list[Entry]:
        if self.has("list_sizes"):
            text = self.call("list_sizes", {"path": str(p)})
            out = []
            for ln in text.splitlines():
                m = re.match(r"\[(DIR|FILE)\]\s+(.+?)\s{2,}(\S.*)?$", ln) or re.match(r"\[(DIR|FILE)\]\s+(.+?)\s*$", ln)
                if m:
                    out.append(Entry(m.group(2).strip(), m.group(1) == "DIR", _parse_size(m.group(3) if m.lastindex and m.lastindex >= 3 else None)))
            return out
        text = self.call("list", {"path": str(p)})
        return [Entry(ln[6:].strip() if ln.startswith("[DIR]") else ln[7:].strip(), ln.startswith("[DIR]"))
                for ln in text.splitlines() if ln.startswith(("[DIR]", "[FILE]"))]

    def tree(self, p: Path, depth: int | None, exclude: list[str]) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"path": str(p), "excludePatterns": exclude}
        if depth is not None:
            args["depth"] = int(depth)
        return json.loads(self.call("tree", args))

    def search(self, p: Path, pattern: str, exclude: list[str]) -> list[str]:
        text = self.call("search", {"path": str(p), "pattern": pattern, "excludePatterns": exclude})
        if text.strip() == "No matches found":
            return []
        return [ln for ln in text.split("\n\nThe search is incomplete")[0].splitlines() if ln.strip()]

    def info(self, p: Path) -> str:
        return self.call("info", {"path": str(p)})

    def write(self, p: Path, content: str) -> str:
        if self.has("mkdir"):
            self.call("mkdir", {"path": str(p.parent)})     # server requires an existing parent
        return self.call("write", {"path": str(p), "content": content})

    def append(self, p: Path, content: str) -> str:
        if self.has("mkdir"):
            self.call("mkdir", {"path": str(p.parent)})
        return self.call("append", {"path": str(p), "content": content})

    def edit(self, p: Path, edits: list[dict[str, str]], dry_run: bool = False) -> str:
        return self.call("edit", {"path": str(p), "edits": edits, "dryRun": bool(dry_run)})

    def mkdir(self, p: Path) -> str:
        return self.call("mkdir", {"path": str(p)})

    def move(self, src: Path, dst: Path) -> str:
        if self.has("mkdir"):
            self.call("mkdir", {"path": str(dst.parent)})
        return self.call("move", {"source": str(src), "destination": str(dst)})

    def copy(self, src: Path, dst: Path) -> str:
        if self.has("mkdir"):
            self.call("mkdir", {"path": str(dst.parent)})
        return self.call("copy", {"source": str(src), "destination": str(dst)})


class FsRouter:
    """Routes each operation to MCP or local according to the backend mode."""

    def __init__(self, mode: str, mcp: McpFs | None, fallback_to_local: bool = True) -> None:
        self.mode = mode if mode in {"mcp", "auto", "local"} else "auto"
        self.mcp = mcp
        self.local = LocalFs()
        self.fallback = fallback_to_local
        self.stats = FsStats()

    def backend_for(self, op: str, *paths: Path):
        if self.mode == "local" or self.mcp is None:
            if self.mode == "mcp":
                raise ToolError("filesystem backend is 'mcp' but the GoModel filesystem server is not connected")
            return self.local
        ok = self.mcp.has(op) and all(self.mcp.covers(p) for p in paths)
        if ok:
            return self.mcp
        why = (f"`{self.mcp.slug}_{MCP_OPS[op]}` is not exposed by GoModel (add it to allowed_tools)"
               if not self.mcp.has(op) else f"path outside the MCP server's allowed directories: {paths[0]}")
        if self.mode == "mcp":
            raise ToolError(f"filesystem backend is 'mcp' but {why}")
        if why not in self.stats.fallbacks:
            self.stats.fallbacks.append(why)
        return self.local

    def run(self, op: str, method: str, *paths: Path, **kw: Any):
        be = self.backend_for(op, *paths)
        if be is self.mcp:
            self.stats.mcp_calls += 1
        else:
            self.stats.local_calls += 1
        return getattr(be, method)(*paths, **kw), be.name

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {"mode": self.mode}
        if self.mcp is not None:
            d["mcp_server"] = self.mcp.slug
            d["mcp_tools"] = sorted(self.mcp.exposed)
            d["mcp_roots"] = self.mcp.roots
            d["writes_via"] = "mcp" if all(self.mcp.has(o) for o in ("write", "append", "edit")) else \
                ("local (write tools not whitelisted in GoModel)" if self.mode == "auto" else "unavailable")
        else:
            d["mcp_server"] = None
        d["calls"] = {"mcp": self.stats.mcp_calls, "local": self.stats.local_calls}
        if self.stats.fallbacks:
            d["fallbacks"] = self.stats.fallbacks
        return d


# ----------------------------------------------------------------------- helpers
def _within(path: str, root: str) -> bool:
    if re.match(r"^[A-Za-z]:[\\/]", root) or re.match(r"^[A-Za-z]:[\\/]", path):
        p, r = PureWindowsPath(path), PureWindowsPath(root)
        return p == r or str(p).lower().startswith(str(r).lower().rstrip("\\") + "\\")
    p, r = Path(path), Path(root)
    return p == r or p.is_relative_to(r)


def _parse_size(text: str | None) -> int | None:
    if not text:
        return None
    m = re.match(r"([\d.]+)\s*(B|KB|MB|GB|TB)", text.strip())
    if not m:
        return None
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}[m.group(2)]
    return int(float(m.group(1)) * mult)


def _mini_diff(a: str, b: str, name: str) -> str:
    import difflib
    diff = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{name}", f"b/{name}", n=2))
    return diff[:6000] or "(no changes)"
