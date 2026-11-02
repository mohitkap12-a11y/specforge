"""Sign-in, access requests approved by an admin, admin-only routes, user management."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app(ws, monkeypatch):
    monkeypatch.setenv("SPECFORGE_AUTH", "1")
    from specforge import auth
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    auth.create_user(get_conn(), "root", "rootpass1", role="admin", status="active")
    return create_app()


def client(app):
    return TestClient(app, base_url="http://sf.test")


def login(c, user, pw):
    return c.post("/api/auth/login", json={"username": user, "password": pw})


def test_api_requires_sign_in_but_ui_and_me_do_not(app):
    with client(app) as c:
        assert c.get("/api/specs").status_code == 401
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/auth/me").json() == {"auth": True, "user": None, "pending": 0}
        assert c.get("/api/logs/stream").status_code == 401


def test_request_approve_sign_in_flow(app):
    with client(app) as c:
        r = c.post("/api/auth/register", json={"username": "bob", "password": "bobpass12", "display_name": "Bob", "note": "QA team"})
        assert r.status_code == 200 and r.json()["status"] == "pending"
        assert c.post("/api/auth/register", json={"username": "BOB", "password": "bobpass12"}).status_code == 409   # names ignore case
        r = login(c, "bob", "bobpass12")
        assert r.status_code == 403 and "waiting" in r.json()["detail"]
        assert login(c, "bob", "wrong-pass").json()["detail"] == "wrong username or password"   # status shown only with the right password

        admin = client(app)
        assert login(admin, "root", "rootpass1").status_code == 200
        assert admin.get("/api/auth/me").json()["pending"] == 1
        bob = next(u for u in admin.get("/api/users").json() if u["username"] == "bob")
        assert bob["status"] == "pending" and bob["note"] == "QA team" and "password_hash" not in bob
        assert admin.patch(f"/api/users/{bob['id']}", json={"status": "active"}).json()["decided_by"] == "root"

        assert login(c, "bob", "bobpass12").status_code == 200
        assert c.get("/api/specs").status_code == 200
        me = c.get("/api/auth/me").json()
        assert me["user"]["username"] == "bob" and me["user"]["role"] == "user"

        logs = admin.get("/api/logs", params={"q": "approved"}).json()["items"]
        assert any(e["data"].get("user") == "root" for e in logs)


def test_admin_only_routes(app):
    from specforge import auth
    from specforge.core.db import get_conn

    auth.create_user(get_conn(), "carol", "carolpass", status="active")
    with client(app) as c:
        login(c, "carol", "carolpass")
        assert c.get("/api/settings").status_code == 200
        assert c.put("/api/settings", json={}).status_code == 403
        assert c.post("/api/reset").status_code == 403
        assert c.delete("/api/logs").status_code == 403
        assert c.get("/api/users").status_code == 403
        assert c.patch("/api/users/1", json={"role": "user"}).status_code == 403


def test_disable_and_password_reset_end_sessions(app):
    with client(app) as admin, client(app) as c:
        login(admin, "root", "rootpass1")
        uid = admin.post("/api/users", json={"username": "dave", "password": "davepass1"}).json()["id"]
        assert login(c, "dave", "davepass1").status_code == 200
        admin.post(f"/api/users/{uid}/password", json={"password": "newpass99"})
        assert c.get("/api/specs").status_code == 401
        assert login(c, "dave", "newpass99").status_code == 200
        admin.patch(f"/api/users/{uid}", json={"status": "disabled"})
        assert c.get("/api/specs").status_code == 401
        assert "disabled" in login(c, "dave", "newpass99").json()["detail"]


def test_change_own_password_keeps_this_session(app):
    with client(app) as a, client(app) as b:
        login(a, "root", "rootpass1"); login(b, "root", "rootpass1")
        assert a.post("/api/auth/password", json={"current": "nope", "new": "rootpass2"}).status_code == 403
        assert a.post("/api/auth/password", json={"current": "rootpass1", "new": "short"}).status_code == 422
        assert a.post("/api/auth/password", json={"current": "rootpass1", "new": "rootpass2"}).status_code == 200
        assert a.get("/api/specs").status_code == 200
        assert b.get("/api/specs").status_code == 401


def test_admin_cannot_lock_out_the_server(app):
    with client(app) as c:
        login(c, "root", "rootpass1")
        root = c.get("/api/auth/me").json()["user"]["id"]
        assert c.patch(f"/api/users/{root}", json={"role": "user"}).status_code == 409
        assert c.patch(f"/api/users/{root}", json={"status": "disabled"}).status_code == 409
        assert c.delete(f"/api/users/{root}").status_code == 409
        other = c.post("/api/users", json={"username": "eve", "password": "evepass12", "role": "admin"}).json()["id"]
        assert c.delete(f"/api/users/{other}").status_code == 200


def test_logout_and_cross_origin_writes(app):
    with client(app) as c:
        login(c, "root", "rootpass1")
        assert c.put("/api/settings", json={}, headers={"Origin": "http://evil.test"}).status_code == 403
        assert c.put("/api/settings", json={}, headers={"Origin": "http://sf.test"}).status_code == 200
        c.post("/api/auth/logout")
        assert c.get("/api/specs").status_code == 401


def test_failed_logins_are_throttled(app):
    with client(app) as c:
        for _ in range(10):
            assert login(c, "root", "bad-password").status_code == 401
        assert login(c, "root", "rootpass1").status_code == 429


def test_passwords_are_hashed():
    from specforge import auth

    h = auth.hash_password("correct horse")
    assert "correct horse" not in h and auth.verify_password("correct horse", h) and not auth.verify_password("wrong", h)
    assert h != auth.hash_password("correct horse")        # salted


def test_no_auth_mode_is_local_admin(ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/specs").status_code == 200
        me = c.get("/api/auth/me").json()
        assert me["auth"] is False and me["user"]["role"] == "admin"
        assert c.post("/api/auth/register", json={"username": "x" * 4, "password": "y" * 8}).status_code == 409


def test_null_origin_writes_are_refused(app, ws, monkeypatch):
    with client(app) as c:
        login(c, "root", "rootpass1")
        assert c.post("/api/sets", json={"name": "x", "document_ids": []}, headers={"Origin": "null"}).status_code == 403
    monkeypatch.setenv("SPECFORGE_AUTH", "0")             # no cookie to protect, so the origin check is all there is
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        assert c.post("/api/reset", headers={"Origin": "null"}).status_code == 403


def test_extended_session_resends_the_cookie(app):
    from specforge.core.db import get_conn

    with client(app) as c:
        login(c, "root", "rootpass1")
        assert "set-cookie" not in c.get("/api/specs").headers          # within the minute: nothing to extend
        get_conn().execute("UPDATE user_session SET last_seen='2000-01-01 00:00:00'")
        get_conn().commit()
        r = c.get("/api/specs")
        assert r.status_code == 200 and "sf_session=" in r.headers["set-cookie"] and "Max-Age=604800" in r.headers["set-cookie"]


def test_admin_resetting_own_password_stays_signed_in(app):
    with client(app) as c, client(app) as other:
        login(c, "root", "rootpass1"); login(other, "root", "rootpass1")
        me = c.get("/api/auth/me").json()["user"]["id"]
        assert c.post(f"/api/users/{me}/password", json={"password": "rootpass9"}).status_code == 200
        assert c.get("/api/specs").status_code == 200
        assert other.get("/api/specs").status_code == 401


def test_throttle_keys_on_the_proxied_client_address():
    from starlette.requests import Request

    from specforge.api.app import client_ip

    def req(peer, fwd=None):
        headers = [(b"x-forwarded-for", fwd.encode())] if fwd else []
        return Request({"type": "http", "client": (peer, 1), "headers": headers})

    assert client_ip(req("127.0.0.1", "10.0.0.9, 10.1.2.3")) == "10.1.2.3"      # rightmost: what the local proxy saw
    assert client_ip(req("10.1.2.3", "1.2.3.4")) == "10.1.2.3"                 # not from a local proxy: header ignored
    assert client_ip(req("127.0.0.1")) == "127.0.0.1"


def test_sign_in_off_needs_a_localhost_host(ws, monkeypatch):
    from specforge import cli

    monkeypatch.setenv("SPECFORGE_AUTH", "0")
    with pytest.raises(SystemExit, match="localhost"):
        cli.main(["serve", "--host", "0.0.0.0"])
    monkeypatch.setenv("SPECFORGE_AUTH", "1")
    with pytest.raises(SystemExit, match="localhost"):
        cli.main(["serve", "--host", "0.0.0.0", "--no-auth"])


def test_coverage_tool_is_admin_only_until_released(app):
    from specforge import auth
    from specforge.core.db import get_conn

    auth.create_user(get_conn(), "erin", "erinpass1", status="active")
    with client(app) as admin, client(app) as c:
        login(admin, "root", "rootpass1"); login(c, "erin", "erinpass1")
        assert c.get("/api/auth/me").json()["features"] == {"coverage_released": False}
        for path in ("/api/sets", "/api/cmddb", "/api/coverage/runs", "/api/cross/angles"):
            assert c.get(path).status_code == 403, path
            assert admin.get(path).status_code == 200, path
        assert c.post("/api/sets", json={"name": "x", "document_ids": []}).status_code == 403
        # the converter, Compare and the spec library stay open to everyone
        assert c.get("/api/specs").status_code == 200
        assert c.get("/api/compare").status_code == 200
        # only an admin releases it
        assert c.put("/api/features", json={"coverage_released": True}).status_code == 403
        assert admin.put("/api/features", json={"coverage_released": "yes"}).status_code == 422
        assert admin.put("/api/features", json={"other": True}).status_code == 422
        assert admin.put("/api/features", json={"coverage_released": True}).json() == {"coverage_released": True}
        assert c.get("/api/auth/me").json()["features"] == {"coverage_released": True}
        assert c.get("/api/sets").status_code == 200
        # saving the settings form (loaded before the rollout) does not undo it
        stale = admin.get("/api/settings").json()
        stale["features"] = {"coverage_released": False}
        assert admin.put("/api/settings", json=stale).status_code == 200
        assert c.get("/api/sets").status_code == 200
        # and it can be taken back to an admin-only preview
        assert admin.put("/api/features", json={"coverage_released": False}).status_code == 200
        assert c.get("/api/sets").status_code == 403


def test_no_auth_local_admin_always_has_coverage(ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/sets").status_code == 200
