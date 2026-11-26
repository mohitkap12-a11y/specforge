"""Ollama HTTP provider (local). Uses /api/tags, /api/chat (structured output via `format`), /api/embed."""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import httpx

from .base import LLMError, ModelInfo


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", num_ctx: int = 16384, timeout: float = 600.0,
                 client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.client = client or httpx.Client(timeout=timeout)

    def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.client.request(method, self.base_url + path, **kw)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request {path} failed: {e}") from e

    def list_models(self) -> list[ModelInfo]:
        data = self._req("GET", "/api/tags")
        out = []
        for m in data.get("models", []):
            det = m.get("details", {})
            out.append(ModelInfo(id=m["name"], provider=self.name, size=str(m.get("size", "")),
                                 detail=f"{det.get('parameter_size', '')} {det.get('quantization_level', '')}".strip()))
        return out

    def health(self) -> dict[str, Any]:
        try:
            models = self.list_models()
            try:
                ps = self._req("GET", "/api/ps")
            except LLMError:
                ps = {}
            return {"ok": True, "models": len(models), "loaded": [m.get("name") for m in ps.get("models", [])]}
        except LLMError as e:
            return {"ok": False, "error": str(e)}

    def generate(self, messages, *, model, schema=None, temperature=0.0, max_tokens=None) -> str:
        if not model:
            raise LLMError("No Ollama model selected (Settings -> Models)")
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False,
                                "options": {"temperature": temperature, "num_ctx": self.num_ctx}}
        if max_tokens:
            body["options"]["num_predict"] = max_tokens
        if schema is not None:
            body["format"] = schema
        data = self._req("POST", "/api/chat", json=body)
        try:
            return data["message"]["content"]
        except KeyError as e:
            raise LLMError(f"Unexpected Ollama response: {str(data)[:200]}") from e

    def stream(self, messages, *, model, temperature=0.2, max_tokens=None, think=None) -> Iterator[str]:
        """Yield the reply's text as the model writes it. `last_stats` holds tokens and speed once the stream has ended."""
        if not model:
            raise LLMError("No Ollama model selected (Settings -> Chatbot)")
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True,
                                "options": {"temperature": temperature, "num_ctx": self.num_ctx}}
        if max_tokens:
            body["options"]["num_predict"] = max_tokens
        if think is not None:
            body["think"] = think
        self.last_stats: dict[str, Any] = {}
        t0 = time.monotonic()
        try:
            with self.client.stream("POST", self.base_url + "/api/chat", json=body) as r:
                if r.status_code >= 400:
                    r.read()
                    raise LLMError(f"Ollama /api/chat failed: {r.status_code} {r.text[:300]}")
                for line in r.iter_lines():
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev.get("error"):
                        raise LLMError(f"Ollama: {ev['error']}")
                    if piece := (ev.get("message") or {}).get("content"):
                        yield piece
                    if ev.get("done"):
                        n, ns = ev.get("eval_count"), ev.get("eval_duration")
                        self.last_stats = {"tokens": n, "prompt_tokens": ev.get("prompt_eval_count"),
                                           "tps": round(n / (ns / 1e9), 1) if n and ns else None}
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request failed: {e}") from e
        self.last_stats.setdefault("seconds", round(time.monotonic() - t0, 2))

    def embed(self, texts, *, model) -> list[list[float]]:
        data = self._req("POST", "/api/embed", json={"model": model, "input": texts})
        return data["embeddings"]
