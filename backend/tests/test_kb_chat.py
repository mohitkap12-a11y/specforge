"""Knowledge base (chunking, search, adding conversions) and the chatbot (sessions, streaming, follow-ups)."""
import json

import pytest
from fastapi.testclient import TestClient

from specforge import chat, knowledge


@pytest.fixture()
def client(ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


DOC = """---
title: NVMe Test Spec
---
# Identify
The Identify command returns a data structure about the controller.

## Fields
| Field | Bits |
|---|---|
| CNS | 7:0 |
| NSID | 31:0 |

```c
int cns = 1;
```

# Queues
SQ_ID is the submission queue identifier. A submission queue holds commands.
"""


class FakeProvider:
    name = "fake"

    def __init__(self, pieces, fail=False):
        self.pieces, self.fail, self.calls, self.last_stats = pieces, fail, [], {}

    def stream(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.fail:
            raise chat.LLMError("model server down")
        yield from self.pieces
        self.last_stats = {"tokens": 7, "tps": 55.0}

    def generate(self, messages, **kw):
        return "1. What is CNS?\n2. Which bits hold NSID?\n3. How is Identify issued?"


def test_chunking_keeps_tables_and_headings():
    chunks = knowledge.chunk_markdown(DOC)
    assert [h for h, _ in chunks] == ["Identify", "Identify › Fields", "Queues"]
    assert "| CNS | 7:0 |" in chunks[1][1] and "int cns = 1;" in chunks[1][1]
    big = "# T\n\n| a | b |\n|---|---|\n" + "\n".join(f"| r{i} | {'x' * 80} |" for i in range(60))
    pieces = knowledge.chunk_markdown(big, 800)
    assert len(pieces) > 3 and all(p[1].startswith("| a | b |") for p in pieces)


def test_to_markdown_variants():
    assert knowledge.to_markdown("a.html", b"<h1>Hi</h1><p>x &amp; y</p><script>bad()</script>").startswith("# Hi")
    assert knowledge.to_markdown("a.json", b'{"a":1}').startswith("```json")
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.to_markdown("a.exe", b"MZ")


def test_add_search_and_duplicates(conn):
    d = knowledge.add_markdown(conn, DOC, filename="t.md", source="upload", user="u")
    assert d["title"] == "NVMe Test Spec" and d["chunks"] == 3
    assert knowledge.search(conn, "What is the SQ_ID submission queue?")[0]["heading"] == "Queues"
    assert knowledge.search(conn, "identify command CNS")[0]["heading"].startswith("Identify")
    assert knowledge.search(conn, "zzzz unknown") == []
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.add_markdown(conn, DOC, filename="again.md", source="upload", user="u")
    knowledge.set_enabled(conn, d["id"], False)
    assert knowledge.search(conn, "SQ_ID") == []
    assert knowledge.remove(conn, d["id"]) and knowledge.listing(conn) == []


def test_kb_api_and_pending_conversions(client):
    up = client.post("/api/kb/upload", files=[("files", ("a.md", DOC.encode(), "text/markdown")), ("files", ("b.bin", b"\x00\x01", "x"))]).json()
    assert len(up["added"]) == 1 and "unsupported" in up["failed"][0]["error"]
    kb = client.get("/api/kb").json()
    assert kb["chunks"] == 3 and kb["pending"] == []
    assert client.get("/api/kb/search", params={"q": "NSID"}).json()[0]["doc_id"] == kb["docs"][0]["id"]
    doc = client.get(f"/api/kb/{kb['docs'][0]['id']}").json()
    assert "Identify" in doc["markdown"]
    assert client.patch(f"/api/kb/{doc['id']}", json={"enabled": 1}).status_code == 422
    assert client.delete(f"/api/kb/{doc['id']}").status_code == 200
    assert client.get("/api/kb").json()["docs"] == []


def test_conversions_are_tracked_and_added_in_one_go(client, monkeypatch):
    # a finished conversion in the To Markdown library that is not in the knowledge base yet
    c = client.post("/api/convert/markdown/jobs", files={"file": ("spec.docx", _docx(), "application/octet-stream")})
    job = c.json()["job"]
    import time
    for _ in range(300):
        if client.get(f"/api/jobs/{job}").json()["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert [p["job"] for p in client.get("/api/kb").json()["pending"]] == [job]
    entry = next(e for e in client.get("/api/convert/markdown/jobs").json() if e["job"] == job)
    assert entry["kb_doc"] is None
    r = client.post("/api/kb/from-conversions", json={}).json()
    assert len(r["added"]) == 1 and r["pending"] == 0
    entry = next(e for e in client.get("/api/convert/markdown/jobs").json() if e["job"] == job)
    assert entry["kb_doc"] == r["added"][0]["id"]
    assert client.post("/api/kb/from-conversions", json={}).json()["added"] == []
    assert client.post("/api/kb/from-conversions", json={"jobs": ["x"]}).status_code == 422


def _docx():
    import io

    import docx
    d = docx.Document()
    d.add_heading("Hello Spec", 1)
    d.add_paragraph("The Format NVM command formats a namespace.")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def events(resp):
    return [json.loads(x) for x in resp.text.splitlines() if x]


def test_stream_parser_hides_thinking_and_splits_followups():
    p = chat.StreamParser()
    out = "".join(p.feed(x) for x in ["<thi", "nk>secret</think>\nHel", "lo <<FOLLOW", "UPS>>\nWhat next?\nAnd then?"]) + p.finish()
    assert out == "Hello " and parse(p.followups) == ["What next?", "And then?"]
    q = chat.StreamParser()
    assert "".join(q.feed(x) for x in ["a < b and ", "<<", " x"]) + q.finish() == "a < b and << x"


def parse(s):
    return chat.parse_followups(s)


def test_chat_session_flow(client, monkeypatch):
    client.put("/api/settings", json={"chat": {"model": "qwen3:8b"}})
    client.post("/api/kb/upload", files=[("files", ("a.md", DOC.encode(), "text/markdown"))])
    fake = FakeProvider(["The Identify command ", "returns data [1].", "\n<<FOLLOWUPS>>\nWhat is CNS?\nWhich bits are NSID?\nHow is it sent?\n"])
    monkeypatch.setattr("specforge.api.app.make_provider", lambda name, s: fake)
    sid = client.post("/api/chat/sessions").json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "What does Identify return?"})
    ev = events(r)
    assert ev[0]["type"] == "start" and ev[-1]["type"] == "done" and {e["type"] for e in ev[1:-1]} == {"delta"}
    assert ev[0]["sources"][0]["heading"] == "Identify" and ev[0]["title"] == "What does Identify return?"
    done = ev[-1]["message"]
    assert done["content"] == "The Identify command returns data [1]." and done["suggestions"] == ["What is CNS?", "Which bits are NSID?", "How is it sent?"]
    assert done["stats"]["tps"] == 55.0
    sent = fake.calls[0][0]
    assert sent[-1]["content"].startswith("CONTEXT:\n[1] NVMe Test Spec › Identify") and "QUESTION:\nWhat does Identify return?" in sent[-1]["content"]
    assert fake.calls[0][1]["think"] is False
    # a second question carries the history
    client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "and its fields?"})
    assert [m["role"] for m in fake.calls[1][0]] == ["system", "user", "assistant", "user"]
    s = client.get(f"/api/chat/sessions/{sid}").json()
    assert [m["role"] for m in s["messages"]] == ["user", "assistant"] * 2
    # regenerate replaces the last answer
    client.post(f"/api/chat/sessions/{sid}/messages", json={"regenerate": True})
    s = client.get(f"/api/chat/sessions/{sid}").json()
    assert len(s["messages"]) == 4
    mid = s["messages"][1]["id"]
    assert client.patch(f"/api/chat/messages/{mid}", json={"feedback": 1}).status_code == 200
    assert client.get("/api/chat/sessions", params={"q": "fields"}).json()[0]["id"] == sid
    assert "# What does Identify return?" in client.get(f"/api/chat/sessions/{sid}/export").text
    assert client.patch(f"/api/chat/sessions/{sid}", json={"title": "Renamed", "pinned": True}).json()["pinned"] == 1
    assert client.delete(f"/api/chat/sessions/{sid}").status_code == 200
    assert client.get(f"/api/chat/sessions/{sid}").status_code == 404


def test_chat_fallback_suggestions_and_errors(client, monkeypatch):
    client.put("/api/settings", json={"chat": {"model": "m"}})
    fake = FakeProvider(["Plain answer without marker."])
    monkeypatch.setattr("specforge.api.app.make_provider", lambda name, s: fake)
    sid = client.post("/api/chat/sessions").json()["id"]
    ev = events(client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "hello there"}))
    assert [e["type"] for e in ev] == ["start", "delta", "done", "suggestions"] and len(ev[-1]["items"]) == 3
    assert client.get(f"/api/chat/sessions/{sid}").json()["messages"][-1]["suggestions"] == ev[-1]["items"]
    down = FakeProvider([], fail=True)
    monkeypatch.setattr("specforge.api.app.make_provider", lambda name, s: down)
    ev = events(client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "again"}))
    assert ev[-1] == {"type": "error", "message": "model server down"}
    # no model chosen
    client.put("/api/settings", json={"chat": {"model": ""}})
    monkeypatch.setattr("specforge.api.app.make_provider", lambda name, s: fake)
    assert "No chat model" in events(client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "x"}))[-1]["message"]


def test_sessions_are_private_and_chat_can_be_turned_off(client):
    from specforge.core.db import get_conn
    c = get_conn()
    other = chat.create_session(c, "someone-else")
    assert client.get(f"/api/chat/sessions/{other['id']}").status_code == 404
    assert client.delete(f"/api/chat/sessions/{other['id']}").status_code == 404
    assert client.get("/api/chat/sessions").json() == []
    client.put("/api/settings", json={"chat": {"enabled": False}})
    assert client.post("/api/chat/sessions").status_code == 409


def test_backup_round_trip_of_knowledge_base_and_chats(tmp_path, monkeypatch):
    import io

    from specforge.core import db

    def make(home):
        monkeypatch.setenv("SPECFORGE_HOME", str(home))
        db._local.__dict__.clear()
        from specforge.api.app import create_app
        return TestClient(create_app())

    with make(tmp_path / "a") as a:
        a.post("/api/kb/upload", files=[("files", ("a.md", DOC.encode(), "text/markdown"))])
        sid = a.post("/api/chat/sessions").json()["id"]
        a.put("/api/settings", json={"chat": {"model": "m"}})
        monkeypatch.setattr("specforge.api.app.make_provider", lambda n, s: FakeProvider(["Hi"]))
        a.post(f"/api/chat/sessions/{sid}/messages", json={"text": "hello"})
        data = a.get("/api/backup/export", params={"items": "knowledge,chats"}).content
    with make(tmp_path / "b") as b:
        r = b.post("/api/backup/import", files={"file": ("b.zip", io.BytesIO(data), "application/zip")}, data={"items": "knowledge,chats"})
        assert r.status_code == 200, r.text
        assert b.get("/api/kb/search", params={"q": "SQ_ID"}).json()[0]["heading"] == "Queues"      # the index was rebuilt
        assert "Identify" in b.get(f"/api/kb/{b.get('/api/kb').json()['docs'][0]['id']}").json()["markdown"]
        assert [m["role"] for m in b.get(f"/api/chat/sessions/{sid}").json()["messages"]] == ["user", "assistant"]


def test_chat_also_reads_parsed_specs(conn, demo_dir):
    from specforge import specquery
    from specforge.core import config
    from specforge.ingest import store

    d = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    store.run_parse(conn, d["id"], workers=1)
    lib = specquery.SpecLibrary(str(config.db_path()))
    hits = chat.spec_hits(lib, "Identify controller", 4)
    assert hits and hits[0]["doc_id"] is None and hits[0]["title"].startswith("nvme-base") and hits[0]["text"]
    assert chat.spec_hits(lib, "zzzz", 4) == []


def test_disabled_documents_do_not_crowd_out_enabled_ones(conn):
    for i in range(6):
        d = knowledge.add_markdown(conn, "# T\n\n" + ("widget " * 30) + f"copy {i}", filename=f"d{i}.md", source="upload", user="u")
        knowledge.set_enabled(conn, d["id"], False)
    knowledge.add_markdown(conn, "# On\n\nwidget enabled", filename="on.md", source="upload", user="u")
    assert [h["title"] for h in knowledge.search(conn, "widget", 6)] == ["On"]


def test_settings_hide_the_server_api_key_from_non_admins(ws, monkeypatch):
    monkeypatch.setenv("SPECFORGE_AUTH", "1")
    from specforge import auth
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    auth.create_user(get_conn(), "root", "rootpass1", role="admin", status="active")
    auth.create_user(get_conn(), "carol", "carolpass", status="active")
    with TestClient(create_app(), base_url="http://sf.test") as admin, TestClient(create_app(), base_url="http://sf.test") as user:
        admin.post("/api/auth/login", json={"username": "root", "password": "rootpass1"})
        user.post("/api/auth/login", json={"username": "carol", "password": "carolpass"})
        admin.put("/api/settings", json={"openai": {"api_key": "sk-secret"}})
        assert admin.get("/api/settings").json()["openai"]["api_key"] == "sk-secret"
        assert user.get("/api/settings").json()["openai"]["api_key"] == ""
