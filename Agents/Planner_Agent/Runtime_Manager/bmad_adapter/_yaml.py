"""Tiny YAML helpers.

PyYAML is used when installed. Otherwise a small fallback handles what BMAD actually
ships: SKILL.md frontmatter (flat keys, one level of nesting, quoted strings) and the
``agents:`` roster list in module.yaml.
"""

from __future__ import annotations

import re
from typing import Any

try:  # pragma: no cover - exercised implicitly when available
    import yaml as _pyyaml  # type: ignore
except Exception:  # pragma: no cover
    _pyyaml = None

_KEY = re.compile(r"^(\s*)([A-Za-z0-9_.\-]+)\s*:\s*(.*?)\s*$")


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "'\"" and raw[-1] == raw[0] and len(raw) >= 2:
        inner = raw[1:-1]
        return inner.replace("''", "'") if raw[0] == "'" else inner.replace('\\"', '"')
    # strip trailing comment
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    low = raw.lower()
    if low in {"true", "yes"}:
        return True
    if low in {"false", "no"}:
        return False
    if low in {"null", "~"}:
        return None
    if raw.startswith("[") and raw.endswith("]"):
        return [_scalar(p) for p in raw[1:-1].split(",") if p.strip()]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def safe_load(text: str) -> Any:
    if _pyyaml is not None:
        try:
            return _pyyaml.safe_load(text)
        except Exception:
            pass
    return _fallback_load(text)


def _fallback_load(text: str) -> dict[str, Any]:
    """Indentation-based loader for maps, nested maps and lists of maps/scalars."""
    lines = [ln.rstrip("\n") for ln in text.splitlines()]
    root: dict[str, Any] = {}
    # stack of (indent, container)
    stack: list[tuple[int, Any]] = [(-1, root)]
    last_key_at: dict[int, tuple[dict, str]] = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        while len(stack) > 1 and stack[-1][0] >= indent and not (
            stripped.startswith("- ") and isinstance(stack[-1][1], list) and stack[-1][0] == indent
        ):
            stack.pop()
        parent = stack[-1][1]
        if stripped.startswith("- ") or stripped == "-":
            item_text = stripped[1:].strip()
            if not isinstance(parent, list):
                # list attached to the last key at a shallower indent
                owner = _find_owner(last_key_at, indent)
                if owner is None:
                    continue
                d, k = owner
                if not isinstance(d.get(k), list):
                    d[k] = []
                parent = d[k]
                stack.append((indent, parent))
            m = _KEY.match(item_text) if item_text else None
            if m and not item_text.startswith(("'", '"')):
                item: dict[str, Any] = {}
                parent.append(item)
                key, val = m.group(2), m.group(3)
                item[key] = _scalar(val) if val else {}
                stack.append((indent + 1, item))
                last_key_at[indent + 2] = (item, key)
            else:
                parent.append(_scalar(item_text))
            continue
        m = _KEY.match(line)
        if not m:
            continue
        key, val = m.group(2), m.group(3)
        if isinstance(parent, list):
            continue
        if val in {"", "|", ">"}:
            parent[key] = {}
            stack.append((indent, parent[key]))
            last_key_at[indent] = (parent, key)
        else:
            parent[key] = _scalar(val)
            last_key_at[indent] = (parent, key)
    return _collapse_empty(root)


def _find_owner(last_key_at: dict[int, tuple[dict, str]], indent: int):
    for ind in sorted((i for i in last_key_at if i <= indent), reverse=True):
        return last_key_at[ind]
    return None


def _collapse_empty(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _collapse_empty(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_collapse_empty(v) for v in node]
    return node


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body) for a Markdown file with optional YAML frontmatter."""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    body = text[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    data = safe_load(raw) or {}
    return (data if isinstance(data, dict) else {}), body
