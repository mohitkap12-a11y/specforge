import json
import stat

import httpx
import pytest

from specforge.ai import generate_ai_scenarios
from specforge.core import config
from specforge.core.db import rows
from specforge.llm.base import LLMError
from specforge.llm.ollama import OllamaProvider
from specforge.llm.opencode import OpenCodeProvider
from specforge.llm.service import LLMService, extract_json


class Fake:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def generate(self, messages, **kw):
        self.calls += 1
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return r(messages) if callable(r) else r

    def list_models(self): return []
    def health(self): return {"ok": True}
    def embed(self, t, **kw): return []


def settings():
    s = config.deep_merge(config.DEFAULT_SETTINGS, {})
    s["tasks"]["generate"]["model"] = "fake-model"
    return s


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
    from specforge.ai import AIOutput

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


def _reply_for(conn):
    req = next(r for r in rows(conn, "SELECT * FROM catalog_item WHERE kind='requirement'") if "Invalid Field in Command if a reserved" in r["label"])
    text = json.loads(req["attrs_json"])["text"]
    step = {"command": "cmd:admin:06", "fields": [{"dw": 10, "hi": 15, "lo": 8, "op": "ne", "value": 0}]}
    base = {"title": "Identify with reserved CDW10[15:08] set", "observability": "stimulus", "steps": [step], "quotes": [text[:60]]}
    return req, base


def test_ai_scenarios_are_grounded_validated_and_drafts(conn, parsed_set):
    req, base = _reply_for(conn)

    def reply(messages):
        # one good scenario, one with an invented quote, one with a bit range that does not exist, one duplicate of the first
        bad_quote = {**base, "title": "bad quote", "quotes": ["the controller shall levitate above the namespace"]}
        bad_bits = {**base, "title": "bad bits", "steps": [{"command": "cmd:admin:06", "fields": [{"dw": 12, "hi": 3, "lo": 0, "op": "eq", "value": 1}]}]}
        unknown_cmd = {**base, "title": "unknown", "steps": [{"command": "cmd:admin:EE", "fields": []}]}
        return json.dumps({"testable": True, "scenarios": [base, bad_quote, bad_bits, unknown_cmd, base]})

    svc = LLMService(conn, settings(), Fake([reply]))
    stats = generate_ai_scenarios(conn, 1, svc, [req["key"]])
    assert (stats["created"], stats["rejected"], stats["duplicates"]) == (1, 3, 1)
    sc = rows(conn, "SELECT * FROM scenario WHERE origin='ai'")
    assert len(sc) == 1 and sc[0]["state"] == "draft"
    pred = json.loads(sc[0]["predicate_json"])["steps"][0]
    assert pred["opcode"] == 6 and pred["queue"] == "admin"                  # opcode comes from the catalog, never from the model
    assert conn.execute("SELECT item_key FROM scenario_item WHERE scenario_id=?", (sc[0]["id"],)).fetchone()[0] == req["key"]
    enr = rows(conn, "SELECT state,error FROM enrichment")[0]
    assert enr["state"] == "ok" and "ungrounded" in enr["error"]


def test_ai_not_testable_and_failures_are_recorded(conn, parsed_set):
    req, _ = _reply_for(conn)
    svc = LLMService(conn, settings(), Fake(['{"testable": false, "reason": "controller internal"}']))
    assert generate_ai_scenarios(conn, 1, svc, [req["key"]])["not_testable"] == 1
    assert rows(conn, "SELECT COUNT(*) AS n FROM scenario WHERE origin='ai'")[0]["n"] == 0
    conn.execute("DELETE FROM enrichment"); conn.execute("DELETE FROM llm_cache"); conn.commit()
    svc2 = LLMService(conn, {**settings(), "llm": {"max_retries": 0}}, Fake(["garbage"]))
    assert generate_ai_scenarios(conn, 1, svc2, [req["key"]])["failed"] == 1
    assert rows(conn, "SELECT state FROM enrichment")[0]["state"] == "failed"
