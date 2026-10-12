"""LLMService: provider selection, response cache, JSON extraction, schema validation and repair loop."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.hashing import sha256_text
from .base import LLMError, LLMProvider
from .ollama import OllamaProvider
from .opencode import OpenCodeProvider

T = TypeVar("T", bound=BaseModel)


def make_provider(name: str, settings: dict) -> LLMProvider:
    if name == "ollama":
        o = settings["ollama"]
        return OllamaProvider(o["base_url"], o.get("num_ctx", 16384), o.get("timeout", 600))
    if name == "opencode":
        o = settings["opencode"]
        return OpenCodeProvider(o.get("binary", "opencode"), o.get("timeout", 600))
    raise LLMError(f"Unknown provider '{name}'")


def extract_json(text: str) -> Any:
    """Parse JSON from model output: whole text, fenced block, or first balanced {...}."""
    t = text.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    raise LLMError("No JSON object found in model output")


class LLMService:
    def __init__(self, conn: sqlite3.Connection, settings: dict, provider: LLMProvider | None = None):
        self.conn = conn
        self.settings = settings
        self._provider = provider

    def task_cfg(self, task: str) -> dict:
        return self.settings["tasks"][task]

    def provider_for(self, task: str) -> LLMProvider:
        return self._provider or make_provider(self.task_cfg(task)["provider"], self.settings)

    def generate_model(self, task: str, messages: list[dict], model_cls: type[T], prompt_version: str) -> tuple[T, dict]:
        """Returns (validated object, meta). Raises LLMError after max_retries failed repairs."""
        cfg = self.task_cfg(task)
        prov = self.provider_for(task)
        schema = model_cls.model_json_schema()
        retries = self.settings["llm"].get("max_retries", 2)
        msgs = list(messages)
        last_err = ""
        for attempt in range(retries + 1):
            key = sha256_text(json.dumps([prov.name, cfg["model"], prompt_version, msgs, schema, cfg.get("temperature", 0)], sort_keys=True))
            hit = self.conn.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
            cached = hit is not None
            text = hit[0] if hit else prov.generate(msgs, model=cfg["model"], schema=schema, temperature=cfg.get("temperature", 0.0))
            try:
                obj = model_cls.model_validate(extract_json(text))
            except (LLMError, ValidationError, ValueError) as e:
                last_err = str(e)[:1200]
                msgs = msgs + [{"role": "assistant", "content": text[:4000]},
                               {"role": "user", "content": "Your answer did not validate. Fix these problems and reply with the corrected JSON only:\n" + last_err}]
                continue
            if not cached:
                self.conn.execute("INSERT OR REPLACE INTO llm_cache(key,provider,model,response) VALUES(?,?,?,?)", (key, prov.name, cfg["model"], text))
                self.conn.commit()
            return obj, {"provider": prov.name, "model": cfg["model"], "attempts": attempt + 1, "cached": cached}
        raise LLMError(f"Output failed validation after {retries + 1} attempts: {last_err}")
