"""LLM client for GoModel's OpenAI-compatible ``/v1/chat/completions`` endpoint.

Standard library only (urllib) so the adapter has no hard dependencies. Supports tool
calling, retries with backoff on 408/409/429/5xx, and a pluggable interface so tests (or
another gateway) can swap in their own client.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import LLMError
from .settings import GoModelSettings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": tc.raw_arguments or json.dumps(tc.arguments)}}
                for tc in self.tool_calls
            ]
        return msg


class ChatClient(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
             model: str | None = None, temperature: float | None = None,
             max_tokens: int | None = None) -> LLMResponse: ...


class GoModelChatClient:
    def __init__(self, settings: GoModelSettings) -> None:
        self.settings = settings
        self.url = settings.base_url.rstrip("/") + "/v1/chat/completions"

    def chat(self, messages, tools=None, *, model=None, temperature=None, max_tokens=None) -> LLMResponse:
        s = self.settings
        body: dict[str, Any] = {"model": model or s.model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        temp = s.temperature if temperature is None else temperature
        if temp is not None:
            body["temperature"] = temp
        mx = max_tokens or s.max_output_tokens
        if mx:
            body["max_tokens"] = mx
        data = self._post(body)
        try:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected GoModel response: {json.dumps(data)[:500]}") from exc
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {"__invalid_json__": raw}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args,
                                  raw_arguments=raw if isinstance(raw, str) else json.dumps(raw)))
        content = msg.get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return LLMResponse(content=content, tool_calls=calls, finish_reason=choice.get("finish_reason"),
                           usage=data.get("usage") or {}, raw=data)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        s = self.settings
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if s.api_key:
            headers["Authorization"] = f"Bearer {s.api_key}"
        last: Exception | None = None
        for attempt in range(s.max_retries + 1):
            req = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=s.request_timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                last = LLMError(f"GoModel HTTP {exc.code}: {detail}")
                if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise last from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = LLMError(f"cannot reach GoModel at {self.url}: {exc}")
            time.sleep(min(2 ** attempt, 20))
        raise last or LLMError("GoModel request failed")
