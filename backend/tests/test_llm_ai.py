import json
import stat

import httpx
import pytest

from specforge.llm.base import LLMError
from specforge.llm.ollama import OllamaProvider
from specforge.llm.opencode import OpenCodeProvider
from specforge.llm.service import LLMService, extract_json

from llm_helpers import Fake, settings


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"a": {"b": [1,2]}}\n```') == {"a": {"b": [1, 2]}}
    assert extract_json('blah {"a": "x}y"} trailing') == {"a": "x}y"}
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_ollama_provider_protocol():
    seen = {}

    def handler(req: httpx.Request):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:30b", "size": 1, "details": {"parameter_size": "30B", "quantization_level": "Q4"}}]})
        if req.url.path == "/api/chat":
            seen.update(json.loads(req.content))
            return httpx.Response(200, json={"message": {"content": '{"ok": true}'}})
        if req.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
        return httpx.Response(404)

    p = OllamaProvider("http://x", num_ctx=32768, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.list_models()[0].id == "qwen3:30b"
    out = p.generate([{"role": "user", "content": "hi"}], model="qwen3:30b", schema={"type": "object"}, temperature=0.1)
    assert out == '{"ok": true}'
    assert seen["options"]["num_ctx"] == 32768 and seen["format"] == {"type": "object"} and seen["stream"] is False
    assert p.embed(["a"], model="m") == [[0.1, 0.2]]
    with pytest.raises(LLMError):
        p.generate([], model="")


def test_opencode_provider_with_stub_binary(tmp_path):
    stub = tmp_path / "opencode"
    stub.write_text('#!/bin/sh\nif [ "$1" = "models" ]; then printf "anthropic/claude-x\\norg/model-y\\n"; exit 0; fi\n'
                    'if [ "$1" = "run" ]; then echo "{\\"echo_model\\": \\"$3\\", \\"cfg\\": $(printf %s "$OPENCODE_CONFIG_CONTENT" | grep -c deny)}"; exit 0; fi\nexit 2\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    p = OpenCodeProvider(str(stub))
    assert [m.id for m in p.list_models()] == ["anthropic/claude-x", "org/model-y"]
    out = json.loads(p.generate([{"role": "user", "content": "x"}], model="org/model-y", schema={"type": "object"}))
    assert out["echo_model"] == "org/model-y" and out["cfg"] >= 1       # tool permissions are denied via inline config
    with pytest.raises(LLMError, match="not found"):
        OpenCodeProvider(str(tmp_path / "missing")).list_models()
    with pytest.raises(LLMError, match="too large"):
        p.generate([{"role": "user", "content": "x" * 200_000}], model="m")


def test_service_repairs_invalid_output_and_caches(conn):
    from specforge.crossgen import AIOutput

    good = '{"testable": false, "reason": "internal"}'
    fake = Fake(["not json", '{"testable": "maybe"}', good])
    svc = LLMService(conn, settings(), fake)
    obj, meta = svc.generate_model("generate", [{"role": "user", "content": "q"}], AIOutput, "v1")
    assert obj.testable is False and meta["attempts"] == 3 and fake.calls == 3
    # repair prompt carried the validation errors
    # second identical request is served from cache (key includes the full message list, so use a fresh service with same first reply)
    fake2 = Fake([good])
    svc2 = LLMService(conn, settings(), fake2)
    svc2.generate_model("generate", [{"role": "user", "content": "q2"}], AIOutput, "v1")
    svc2.generate_model("generate", [{"role": "user", "content": "q2"}], AIOutput, "v1")
    assert fake2.calls == 1
    bad = LLMService(conn, settings(), Fake(["nope"]))
    with pytest.raises(LLMError, match="failed validation"):
        bad.generate_model("generate", [{"role": "user", "content": "q3"}], AIOutput, "v1")

