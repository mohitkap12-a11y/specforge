"""Provider-neutral LLM interface."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


@dataclass
class ModelInfo:
    id: str
    provider: str
    size: str = ""
    detail: str = ""


class LLMProvider(Protocol):
    name: str

    def list_models(self) -> list[ModelInfo]: ...

    def generate(self, messages: list[dict[str, str]], *, model: str, schema: dict | None = None,
                 temperature: float = 0.0, max_tokens: int | None = None) -> str: ...

    def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...

    def health(self) -> dict[str, Any]: ...
