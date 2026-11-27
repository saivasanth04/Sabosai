"""Live end-to-end test: GoModel (real Gemini via the `router` virtual model) + MCP + BMAD adapter.

Run by run_live_test.bat (which starts GoModel first). Writes live_test_logs/report.json.
Secrets are read from C:\\Projects\\NTG\\.env and never printed.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
LOGS = HERE / "live_test_logs"
LOGS.mkdir(exist_ok=True)

from bmad_adapter.settings import AdapterSettings, load_dotenv  # noqa: E402

report: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "python": sys.version.split()[0], "steps": []}


def step(name):
    def wrap(fn):
        t0 = time.time()
        rec = {"step": name}
        print(f"\n=== {name}", flush=True)
        try:
            rec.update(fn() or {})
            rec.setdefault("ok", True)
        except Exception as exc:  # noqa: BLE001
            rec.update({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc()[-2500:]})
        rec["seconds"] = round(time.time() - t0, 1)
        print(json.dumps({k: v for k, v in rec.items() if k != "trace"}, indent=1, ensure_ascii=False,
                         default=str)[:3000], flush=True)
        report["steps"].append(rec)
        save()
        return rec
    return wrap


def save():
    (LOGS / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


read = load_dotenv()
report["dotenv_files"] = read
S = AdapterSettings.load()
BASE = S.gomodel.base_url.rstrip("/")
KEY = S.gomodel.api_key
MODEL = S.gomodel.model
report["config"] = {"gateway": BASE, "model": MODEL, "api_key_present": bool(KEY), "mcp_path": S.gomodel.mcp_path}


def http(path, body=None, timeout=120):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {KEY}"} if KEY else {})},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


@step("1. GoModel health")
def _():
    st, body = http("/health")
    return {"status": st, "body": body}


@step("2. GoModel /v1/models (auth with GOMODEL_MASTER_KEY)")
def _():
    st, body = http("/v1/models")
    ids = [m.get("id") for m in body.get("data", [])]
    return {"status": st, "model_count": len(ids), "router_listed": MODEL in ids,
            "sample": ids[:15], "ok": st == 200}


@step(f"3. Chat completion via '{MODEL}' (plain)")
def _():
    from bmad_adapter.llm_client import GoModelChatClient
    c = GoModelChatClient(S.gomodel)
    r = c.chat([{"role": "user", "content": "Reply with exactly: LIVE-OK"}], None, max_tokens=50)
    return {"reply": (r.content or "")[:200], "served_by": r.raw.get("model"), "usage": r.usage,
            "ok": "LIVE-OK" in (r.content or "")}


@step(f"4. Tool calling via '{MODEL}'")
def _():
    from bmad_adapter.llm_client import GoModelChatClient
    c = GoModelChatClient(S.gomodel)
    tool = {"type": "function", "function": {"name": "get_weather", "description": "Get weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
    r = c.chat([{"role": "user", "content": "What's the weather in Chennai? Use the tool."}], [tool], max_tokens=200)
    calls = [{"name": t.name, "args": t.arguments} for t in r.tool_calls]
    return {"tool_calls": calls, "served_by": r.raw.get("model"), "ok": bool(calls)}


@step("5. GoModel MCP gateway: tools/list + one call")
def _():
    from bmad_adapter.mcp_client import GoModelMCPClient
    m = GoModelMCPClient(S.gomodel)
    init = m.initialize()
    tools = m.list_tools()
    names = sorted(t.name for t in tools)
    out = {"server": init.get("serverInfo"), "tool_count": len(names), "tools": names}
    search = next((n for n in names if n.endswith("search") and n.startswith("duckduckgo")), None)
    if search:
        text, err = m.call_tool(search, {"query": "BMAD Method agile AI driven development"})
        out["sample_call"] = {"tool": search, "is_error": err, "preview": text[:400]}
    out["ok"] = len(names) > 0
    return out


@step("6. BMAD adapter: runtime health")
def _():
    from bmad_adapter import BMadRuntime
    project = HERE.parent / "Workspace" / "live-test"      # Agents/Planner_Agent/Workspace/live-test
    rt = BMadRuntime.for_project(project)
    globals()["RT"] = rt
    h = rt.health()
    return {"health": h, "ok": h["registry"]["agent_count"] == 5}


@step("7. BMAD headless skill run: bmad-product-brief (real LLM + tools)")
def _():
    rt = globals().get("RT")
    if rt is None:
        raise RuntimeError("runtime not created")
    calls = []
    rt.engine.on_event = lambda e: (calls.append(e["name"]), print(f"   ↳ {e['name']}", flush=True)) \
        if e.get("type") == "tool_call" else None
    brief = ("intent: create. Product: 'StreakMate', a mobile-first habit tracker for university students with "
             "streaks, gentle reminders and study-group accountability. Stakes: internal pilot. "
             "Keep it short (about one page). Write the brief file and return the status JSON.")
    r = rt.run_skill("bmad-product-brief", brief, headless=True, max_rounds=2)
    files = {a["path"]: a["kind"] for a in rt.artifacts.as_dicts()}
    return {"status": r.status, "completion": r.completion, "artifacts": files, "tool_calls": calls,
            "llm_calls": r.usage.get("llm_calls"), "tokens": r.usage, "error": r.error,
            "last_message": (r.message or "")[:600], "session_id": r.session_id,
            "ok": r.status == "completed" and bool(files)}


report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
report["passed"] = sum(1 for s in report["steps"] if s.get("ok"))
report["total"] = len(report["steps"])
save()
print(f"\nLIVE TEST: {report['passed']}/{report['total']} steps passed. Report: {LOGS / 'report.json'}")
