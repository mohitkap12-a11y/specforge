"""Ollama HTTP provider (local). Uses /api/tags, /api/chat (structured output via `format`), /api/embed."""
from __future__ import annotations

from typing import Any

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

    def embed(self, texts, *, model) -> list[list[float]]:
        data = self._req("POST", "/api/embed", json={"model": model, "input": texts})
        return data["embeddings"]
