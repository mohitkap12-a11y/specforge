"""Backup export / import (specforge/backup.py and /api/backup/*)."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def _client_for(home, monkeypatch):
    monkeypatch.setenv("SPECFORGE_HOME", str(home))
    from specforge.core import db

    db._local.__dict__.clear()
    from specforge.api.app import create_app

    return TestClient(create_app())


def _seed(client, demo_dir):
    docs = client.post("/api/specs/upload", files=[("files", (p.name, p.read_bytes(), "application/pdf"))
                                                  for p in sorted(demo_dir.glob("nvme-base-2.4.pdf"))]).json()
    client.post("/api/sets", json={"name": "demo", "document_ids": [d["id"] for d in docs]})
    return docs


def _export(client, items=None):
    r = client.get("/api/backup/export", params={"items": items} if items else {})
    assert r.status_code == 200, r.text
    return r.content


def _import(client, data, items=None, replace=False):
    form = {"replace": "true" if replace else "false"}
    if items:
        form["items"] = items
    return client.post("/api/backup/import", files={"file": ("b.zip", data, "application/zip")}, data=form)


def _zip(meta, entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("backup_meta.json", json.dumps(meta))
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


def _meta(items, workspace="/old/ws"):
    from specforge.backup import FORMAT_VERSION

    return {"format": FORMAT_VERSION, "items": items, "workspace": workspace}


def test_round_trip_to_a_new_workspace(tmp_path, monkeypatch, demo_dir):
    with _client_for(tmp_path / "a", monkeypatch) as a:
        _seed(a, demo_dir)
        data = _export(a)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        meta = json.loads(z.read("backup_meta.json"))
    assert "db/user_session.jsonl" not in names and "db/mcp_fts.jsonl" not in names
    assert any(n.startswith("files/specs/") for n in names)
    assert meta["workspace"] == str(tmp_path / "a")

    with _client_for(tmp_path / "b", monkeypatch) as b:
        r = _import(b, data, items="specs,sets_catalog")
        assert r.status_code == 200, r.text
        assert r.json()["rows"]["spec_document"] == 1 and r.json()["files"] >= 1
        specs = b.get("/api/specs").json()
        assert len(specs) == 1 and [s["name"] for s in b.get("/api/sets").json()] == ["demo"]
        from specforge.core.db import get_conn

        path = get_conn().execute("SELECT path FROM spec_document").fetchone()[0]
        assert path.startswith(str(tmp_path / "b")) and (tmp_path / "b" / "specs").joinpath(path.rsplit("/", 1)[1]).exists()


def test_export_adds_what_items_depend_on(ws, client):
    data = _export(client, "compare")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert json.loads(z.read("backup_meta.json"))["items"] == ["specs", "compare"]


def test_merge_into_existing_data_is_refused_replace_works(ws, client, demo_dir):
    _seed(client, demo_dir)
    data = _export(client, "specs,sets_catalog")
    r = _import(client, data, items="specs,sets_catalog")
    assert r.status_code == 409 and "Replace existing" in r.json()["detail"]
    r = _import(client, data, items="specs,sets_catalog", replace=True)
    assert r.status_code == 200, r.text
    assert len(client.get("/api/specs").json()) == 1 and len(client.get("/api/sets").json()) == 1


def test_replace_refuses_to_cascade_into_unselected_items(ws, client, demo_dir):
    _seed(client, demo_dir)
    data = _export(client, "specs")
    r = _import(client, data, items="specs", replace=True)
    assert r.status_code == 409 and "Sets, catalog & scenarios" in r.json()["detail"]
    assert len(client.get("/api/sets").json()) == 1


def test_zip_slip_is_rejected_and_nothing_changes(ws, client, tmp_path):
    data = _zip(_meta(["specs"]), {f"db/{t}.jsonl": "" for t in
                                   ("spec_document", "parse_run", "ir_node", "ir_ref", "parse_issue", "correction")}
                | {"files/specs/../../evil.txt": "x"})
    r = _import(client, data)
    assert r.status_code == 400 and "Unsafe path" in r.json()["detail"]
    assert not (ws.parent / "evil.txt").exists() and not list(ws.glob(".backup-import-*"))


def test_missing_parents_roll_back_the_whole_import(ws, client):
    rows = {"spec_document": "", "parse_run": json.dumps({"id": 7, "document_id": 99, "parser_version": "3", "status": "done"}),
            "ir_node": "", "ir_ref": "", "parse_issue": "", "correction": ""}
    data = _zip(_meta(["specs", "llm_cache"]), {f"db/{t}.jsonl": v for t, v in rows.items()}
                | {"db/llm_cache.jsonl": json.dumps({"key": "k", "response": "r"})})
    r = _import(client, data)
    assert r.status_code == 400 and "parse_run" in r.json()["detail"]
    from specforge.core.db import get_conn

    c = get_conn()
    assert c.execute("SELECT COUNT(*) FROM parse_run").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def _plain_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("other.txt", "x")
    return buf.getvalue()


@pytest.mark.parametrize("data,msg", [(b"not a zip", "Not a zip"), (_plain_zip(), "backup_meta")])
def test_bad_files_are_rejected(ws, client, data, msg):
    r = _import(client, data)
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_newer_format_is_refused(ws, client):
    r = _import(client, _zip({"format": 99, "items": ["llm_cache"]}, {"db/llm_cache.jsonl": ""}))
    assert r.status_code == 400 and "format 99" in r.json()["detail"]


def test_unknown_columns_cannot_reach_sql(ws, client):
    row = {"key": "k", "response": "r", 'x") VALUES(1); DROP TABLE job; --': 1}
    r = _import(client, _zip(_meta(["llm_cache"]), {"db/llm_cache.jsonl": json.dumps(row)}))
    assert r.status_code == 200, r.text
    assert any("not in this version" in w for w in r.json()["warnings"])
    from specforge.core.db import get_conn

    assert get_conn().execute("SELECT COUNT(*) FROM job").fetchone()[0] >= 0


def test_users_merge_keeps_existing_accounts(ws, client):
    from specforge import auth
    from specforge.core.db import get_conn

    c = get_conn()
    auth.create_user(c, "alice", "password-1", role="admin", status="active")
    data = _zip(_meta(["users"]), {"db/app_user.jsonl": "\n".join(json.dumps(u) for u in (
        {"id": 1, "username": "ALICE", "password_hash": "other", "role": "user", "status": "active"},
        {"id": 2, "username": "bob", "password_hash": "h", "role": "user", "status": "active"}))})
    r = _import(client, data)
    assert r.status_code == 200, r.text
    users = {u["username"]: u for u in map(dict, c.execute("SELECT * FROM app_user"))}
    assert set(users) == {"alice", "bob"} and users["alice"]["role"] == "admin"
    assert any("already exists" in w for w in r.json()["warnings"])


def test_import_refused_while_jobs_run(ws, client):
    from specforge.core.db import get_conn

    c = get_conn()
    c.execute("INSERT INTO job(type, state) VALUES('parse', 'running')")
    c.commit()
    r = _import(client, _zip(_meta(["llm_cache"]), {"db/llm_cache.jsonl": ""}))
    assert r.status_code == 409 and "still queued or running" in r.json()["detail"]


def test_settings_secrets_are_redacted_and_keep_local_value(ws, client):
    client.put("/api/settings", json={"ollama": {"base_url": "http://example:1", "api_key": "s3cret"}})
    data = _export(client, "settings")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "s3cret" not in z.read("db/setting.jsonl").decode()
    client.put("/api/settings", json={"ollama": {"base_url": "http://other:2", "api_key": "local"}})
    assert _import(client, data).status_code == 200
    s = client.get("/api/settings").json()["ollama"]
    assert s["base_url"] == "http://example:1" and s["api_key"] == "local"


def test_backup_routes_are_admin_only():
    from specforge.api.app import admin_only

    assert all(admin_only(m, p) for m, p in (("GET", "/api/backup/options"), ("GET", "/api/backup/export"),
                                              ("POST", "/api/backup/import")))
