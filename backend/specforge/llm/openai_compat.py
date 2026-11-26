"""OpenAI-compatible HTTP provider: vLLM, llama.cpp `llama-server`, LM Studio, SGLang, TGI, Ollama's /v1 ... Anything that
serves /v1/models and /v1/chat/completions (with server-sent-event streaming)."""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import httpx

from .base import LLMError, ModelInfo


class OpenAICompatProvider:
    name = "openai"

    def __init__(self, base_url: str = "http://127.0.0.1:8000/v1", api_key: str = "", timeout: float = 600.0,
                 client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout)
        self.last_stats: dict[str, Any] = {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.client.request(method, self.base_url + path, headers=self._headers(), **kw)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise LLMError(f"OpenAI-compatible request {path} failed: {e}") from e

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=m["id"], provider=self.name) for m in self._req("GET", "/models").get("data", [])]

    def health(self) -> dict[str, Any]:
        try:
            return {"ok": True, "models": len(self.list_models())}
        except LLMError as e:
            return {"ok": False, "error": str(e)}

    def _body(self, messages, model, temperature, max_tokens, think) -> dict[str, Any]:
        if not model:
            raise LLMError("No model selected (Settings -> Chatbot)")
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if think is False:      # Qwen3 and similar: skip the hidden reasoning for speed (servers that do not know the field ignore it)
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    def generate(self, messages, *, model, schema=None, temperature=0.0, max_tokens=None) -> str:
        body = self._body(messages, model, temperature, max_tokens, None)
        if schema is not None:
            body["response_format"] = {"type": "json_object"}
        data = self._req("POST", "/chat/completions", json=body)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"Unexpected response: {str(data)[:200]}") from e

    def stream(self, messages, *, model, temperature=0.2, max_tokens=None, think=None) -> Iterator[str]:
        body = {**self._body(messages, model, temperature, max_tokens, think), "stream": True, "stream_options": {"include_usage": True}}
        self.last_stats = {}
        first = None
        t0 = time.monotonic()
        try:
            with self.client.stream("POST", self.base_url + "/chat/completions", json=body, headers=self._headers()) as r:
                if r.status_code >= 400:
                    r.read()
                    raise LLMError(f"Chat request failed: {r.status_code} {r.text[:300]}")
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    ev = json.loads(data)
                    if ev.get("usage"):
                        self.last_stats["tokens"] = ev["usage"].get("completion_tokens")
                        self.last_stats["prompt_tokens"] = ev["usage"].get("prompt_tokens")
                    for ch in ev.get("choices") or []:
                        if piece := (ch.get("delta") or {}).get("content"):
                            first = first or time.monotonic()
                            yield piece
        except httpx.HTTPError as e:
            raise LLMError(f"Chat request failed: {e}") from e
        end = time.monotonic()
        n = self.last_stats.get("tokens")
        self.last_stats["tps"] = round(n / (end - first), 1) if n and first and end > first else None
        self.last_stats["seconds"] = round(end - t0, 2)

    def embed(self, texts, *, model) -> list[list[float]]:
        data = self._req("POST", "/embeddings", json={"model": model, "input": texts})
        return [d["embedding"] for d in data["data"]]
