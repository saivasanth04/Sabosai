"""GoModel OpenAI-compatible client. Uses Python's standard library."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GoModelClient:
    def __init__(self, base_url=None, api_key=None, model="router", timeout=None):
        settings = {}
        env_path = Path(__file__).resolve().parents[3] / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                key, separator, value = line.partition("=")
                if separator and not key.strip().startswith("#"):
                    settings[key.strip()] = value.strip().strip("\"'")
        self.base_url = (base_url or os.getenv("GOMODEL_BASE_URL") or
                         "http://localhost:8080/v1").rstrip("/")
        self.api_key = api_key or os.getenv("GOMODEL_MASTER_KEY") or settings.get("GOMODEL_MASTER_KEY")
        self.model = model
        self.timeout = timeout

    def complete(self, messages, schema):
        body = dict(model=self.model, messages=messages, tools=[dict(
            type="function", function=dict(name="product_requirements", parameters=schema))],
            tool_choice=dict(type="function", function=dict(name="product_requirements")),
            parallel_tool_calls=False)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = Request(self.base_url + "/chat/completions",
                          json.dumps(body).encode(), headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"GoModel returned HTTP {exc.code}") from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("GoModel connection failed. Check that the gateway is running.") from exc
        calls = result["choices"][0]["message"].get("tool_calls", [])
        if len(calls) != 1 or calls[0]["function"]["name"] != "product_requirements":
            raise ValueError("Model did not return the required product requirements tool")
        return json.loads(calls[0]["function"]["arguments"])
