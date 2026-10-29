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
