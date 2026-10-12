"""OpenCode CLI provider.

Runs `opencode run --model provider/model "<prompt>"` in an empty temporary directory with a best-effort
no-tools permission config, and returns the printed text.  The CLI has no schema-constrained decoding, so
the schema is spelled out in the prompt and the caller validates + repairs (see service.LLMService).

Flags and config keys follow the OpenCode CLI as documented at the time of writing; the adapter test
(tests/test_llm.py) runs against a stub binary, and `specforge models` / Settings -> Test connection run
against the real one.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

from .base import LLMError, ModelInfo

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MAX_PROMPT_BYTES = 100_000
NO_TOOLS_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
    "autoupdate": False,
    "share": "disabled",
}


class OpenCodeProvider:
    name = "opencode"

    def __init__(self, binary: str = "opencode", timeout: float = 600.0):
        self.binary = binary
        self.timeout = timeout

    def _run(self, args: list[str], cwd: str, timeout: float | None = None) -> str:
        env = dict(os.environ)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(NO_TOOLS_CONFIG)
        env["NO_COLOR"] = "1"
        try:
            p = subprocess.run([self.binary, *args], cwd=cwd, env=env, capture_output=True, text=True,
                               timeout=timeout or self.timeout)
        except FileNotFoundError as e:
            raise LLMError(f"OpenCode binary '{self.binary}' not found (Settings -> OpenCode binary)") from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"OpenCode timed out after {self.timeout}s") from e
        if p.returncode != 0:
            raise LLMError(f"OpenCode exited {p.returncode}: {_ANSI.sub('', p.stderr)[:400]}")
        return _ANSI.sub("", p.stdout)

    def list_models(self) -> list[ModelInfo]:
        with tempfile.TemporaryDirectory(prefix="specforge-oc-") as d:
            out = self._run(["models"], d, timeout=60)
        models = []
        for line in out.splitlines():
            line = line.strip()
            if line and "/" in line and " " not in line:
                models.append(ModelInfo(id=line, provider=self.name))
        return models

    def health(self) -> dict[str, Any]:
        try:
            m = self.list_models()
            return {"ok": True, "models": len(m)}
        except LLMError as e:
            return {"ok": False, "error": str(e)}

    def generate(self, messages, *, model, schema=None, temperature=0.0, max_tokens=None) -> str:
        if not model:
            raise LLMError("No OpenCode model selected (Settings -> Models)")
        parts = []
        for m in messages:
            tag = {"system": "INSTRUCTIONS", "user": "INPUT", "assistant": "YOUR PREVIOUS ANSWER"}.get(m["role"], m["role"].upper())
            parts.append(f"## {tag}\n{m['content']}")
        if schema is not None:
            parts.append("## OUTPUT FORMAT\nReply with ONE JSON object only, no prose and no code fences, matching this JSON Schema:\n"
                         + json.dumps(schema))
        prompt = "\n\n".join(parts)
        if len(prompt.encode()) > MAX_PROMPT_BYTES:
            raise LLMError(f"Prompt too large for the OpenCode CLI ({len(prompt.encode())} bytes); reduce the context size")
        with tempfile.TemporaryDirectory(prefix="specforge-oc-") as d:
            return self._run(["run", "--model", model, prompt], d).strip()

    def embed(self, texts, *, model):
        raise LLMError("OpenCode has no embedding endpoint; configure an Ollama embedding model in Settings")
