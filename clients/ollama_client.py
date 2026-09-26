"""
ModelClient implementation for a local Ollama server (HTTP API).
"""

import json
import time
import urllib.error
import urllib.request

from clients.base import ChatMessage, ModelClient, ModelClientError, ModelResponse


class OllamaClient(ModelClient):
    provider = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout: float = 300):
        # "localhost" first tries IPv6 (::1); Ollama listens on IPv4 only, and Windows
        # waits ~2 s before falling back. Use the IPv4 loopback directly.
        self.base_url = base_url.rstrip("/").replace("://localhost:", "://127.0.0.1:")
        self.timeout = timeout  # seconds; first call may include loading the model

    # --- public API -------------------------------------------------------

    def is_available(self) -> bool:
        try:
            self._request("GET", "/api/version", timeout=5)
            return True
        except ModelClientError:
            return False

    def version(self) -> str:
        return self._request("GET", "/api/version", timeout=5)["version"]

    def list_models(self) -> list[str]:
        data = self._request("GET", "/api/tags", timeout=10)
        return [m["name"] for m in data.get("models", [])]

    def loaded_models(self) -> list[dict]:
        """Models currently loaded in memory, with total and VRAM size in bytes."""
        data = self._request("GET", "/api/ps", timeout=10)
        return [
            {"name": m["name"], "size": m.get("size", 0), "size_vram": m.get("size_vram", 0)}
            for m in data.get("models", [])
        ]

    def unload(self, model: str) -> None:
        """Free the model's (V)RAM now, e.g. before another GPU program runs."""
        self._request("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout=30)

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float | None = None,
        json_schema: dict | None = None,
        think: bool | None = None,
    ) -> ModelResponse:
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        if temperature is not None:
            payload["options"] = {"temperature": temperature}
        if json_schema is not None:
            # Ollama constrains generation so the output matches this schema.
            payload["format"] = json_schema
        if think is not None:
            payload["think"] = think

        start = time.perf_counter()
        data = self._request("POST", "/api/chat", payload)
        elapsed = time.perf_counter() - start

        message = data.get("message") or {}
        return ModelResponse(
            text=(message.get("content") or "").strip(),
            thinking=(message.get("thinking") or "").strip(),
            model=data.get("model", model),
            provider=self.provider,
            duration_seconds=elapsed,
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            raw=data,
        )

    # --- internals --------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # Ollama returns {"error": "..."} for problems like a missing model.
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except json.JSONDecodeError:
                pass
            raise ModelClientError(f"Ollama HTTP {e.code} on {path}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ModelClientError(f"Cannot reach Ollama at {self.base_url}: {e}") from e
        except json.JSONDecodeError as e:
            raise ModelClientError(f"Ollama returned invalid JSON on {path}") from e
