"""Offline mode and the Docling models folder (convert/models.py, core/netguard.py, /api/offline, backup item "models")."""
import io
import json
import os
import socket
import sys
import time
import types
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def restore_network():
    yield
    from specforge.convert import docling_engine, models
    from specforge.core import netguard

    netguard.disable()
    models._set_hf_offline(False)
    docling_engine.configure(None)


@pytest.fixture()
def client(ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def fake_docling(monkeypatch):
    """A stand-in for docling's model downloader, and a pipeline that 'loads' without torch."""
    from specforge.convert import docling_engine

    calls = []

    def download_models(output_dir=None, *, progress=False, with_layout=True, with_easyocr=False):
        calls.append({"output_dir": output_dir, "with_easyocr": with_easyocr})
        (output_dir / "docling-project--docling-layout-heron").mkdir(parents=True, exist_ok=True)
        (output_dir / "docling-project--docling-layout-heron" / "model.safetensors").write_bytes(b"x" * 2048)
    mod = types.ModuleType("docling.utils.model_downloader")
    mod.download_models = download_models
    for name in ("docling", "docling.utils"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name) or types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "docling.utils.model_downloader", mod)
    monkeypatch.setattr(docling_engine, "available", lambda: True)
    loaded = []
    monkeypatch.setattr(docling_engine, "ready", lambda fmt: loaded.append((fmt, docling_engine.artifacts_path())))
    return calls, loaded


def _wait(client, job):
    for _ in range(400):
        j = client.get(f"/api/jobs/{job}").json()
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ------------------------------------------------------------------------------------------------------------ the guard
def test_the_guard_refuses_public_addresses_and_allows_local_ones():
    from specforge.core import netguard

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    netguard.enable()
    try:
        with socket.socket() as s:
            s.connect(("127.0.0.1", port))                 # loopback: allowed
        with socket.socket() as s, pytest.raises(ConnectionRefusedError, match="offline mode"):
            s.connect(("3.168.178.58", 443))
        assert netguard._is_public("::ffff:3.168.178.58") and not netguard._is_public("::1")
        with socket.socket() as s:
            assert s.connect_ex(("8.8.8.8", 53)) != 0
        st = netguard.status()
        assert st["active"] and st["blocked_total"] >= 2 and st["recent"][0]["target"] == "8.8.8.8:53"
        assert not netguard._target_public(socket.socket(), ("10.1.2.3", 11434))      # LAN Ollama
        assert not netguard._target_public(socket.socket(), ("192.168.0.5", 80))
    finally:
        netguard.disable()
        srv.close()
    assert socket.socket.connect is netguard._orig_connect


# ------------------------------------------------------------------------------------------------------------ settings
def test_offline_is_on_by_default_and_applied_at_startup(client):
    from specforge.core import netguard

    st = client.get("/api/offline").json()
    assert st["offline"] is True and st["downloaded"] is None and st["guard"]["active"] is True
    assert os.environ.get("HF_HUB_OFFLINE") == "1" and netguard.enabled()
    assert st["folder"].endswith(os.path.join("models", "docling"))
    assert client.post("/api/offline/download").status_code == 409        # no downloads while offline


def test_switching_offline_off_and_on(client):
    from specforge.core import netguard

    r = client.put("/api/offline", json={"enabled": False}).json()
    assert r["offline"] is False and not netguard.enabled() and "HF_HUB_OFFLINE" not in os.environ
    # the general settings form cannot undo it
    s = client.get("/api/settings").json()
    client.put("/api/settings", json={**s, "offline": {"enabled": True}})
    assert client.get("/api/offline").json()["offline"] is False
    assert client.put("/api/offline", json={"enabled": True}).json()["offline"] is True and netguard.enabled()
    assert client.put("/api/offline", json={"enabled": "yes"}).status_code == 422
    assert client.put("/api/offline", json={"models_path": "relative/dir"}).status_code == 422
    assert client.put("/api/offline", json={"other": 1}).status_code == 422
    logs = json.dumps(client.get("/api/logs").json())
    assert "Offline mode turned off" in logs and "Offline mode turned on" in logs


def test_download_into_the_folder_then_docling_loads_from_it(client, fake_docling, tmp_path):
    from specforge.convert import docling_engine

    calls, loaded = fake_docling
    folder = tmp_path / "models-here"
    client.put("/api/offline", json={"enabled": False, "models_path": str(folder)})
    job = client.post("/api/offline/download").json()["job"]
    j = _wait(client, job)
    assert j["state"] == "done", j
    assert calls[0]["output_dir"] == folder
    assert ("pdf", str(folder)) in loaded and ("docx", str(folder)) in loaded
    st = client.get("/api/offline").json()
    assert st["downloaded"]["files"] == 1 and st["loading_from"] == str(folder) and st["files"] == 2   # model + marker
    client.put("/api/offline", json={"enabled": True})
    assert docling_engine.artifacts_path() == str(folder)      # offline: loads only from the folder


def test_a_download_keeps_the_env_override_in_use(client, fake_docling, monkeypatch, tmp_path):
    from specforge.convert import docling_engine

    _calls, loaded = fake_docling
    monkeypatch.setenv("DOCLING_ARTIFACTS_PATH", str(tmp_path / "override"))
    client.put("/api/offline", json={"enabled": False, "models_path": str(tmp_path / "settings-folder")})
    assert _wait(client, client.post("/api/offline/download").json()["job"])["state"] == "done"
    assert ("pdf", str(tmp_path / "settings-folder")) in loaded          # verified where it was downloaded
    assert docling_engine.artifacts_path() == str(tmp_path / "override")  # but conversions keep the override


def test_env_override_wins(client, monkeypatch, tmp_path):
    from specforge.convert import models

    monkeypatch.setenv("DOCLING_ARTIFACTS_PATH", str(tmp_path / "elsewhere"))
    assert models.effective_artifacts({}) == str(tmp_path / "elsewhere")
    assert client.get("/api/offline").json()["env_override"] == str(tmp_path / "elsewhere")


# ------------------------------------------------------------------------------------------------------------ backup
def test_models_are_a_backup_item_left_out_by_default(client, tmp_path, monkeypatch):
    from specforge.convert import models

    src = tmp_path / "custom-models"
    client.put("/api/offline", json={"models_path": str(src)})
    (src / "layout").mkdir(parents=True)
    (src / "layout" / "w.bin").write_bytes(b"weights")
    (src / models.MARKER).write_text("{}")

    opt = {o["id"]: o for o in client.get("/api/backup/options").json()["items"]}
    assert opt["models"]["default"] is False and opt["settings"]["default"] is True
    with zipfile.ZipFile(io.BytesIO(client.get("/api/backup/export").content)) as z:
        assert not any(n.startswith("files/models/") for n in z.namelist())
    data = client.get("/api/backup/export", params={"items": "models,settings"}).content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "files/models/layout/w.bin" in z.namelist()

    # into a new server whose models folder is the default one
    monkeypatch.setenv("SPECFORGE_HOME", str(tmp_path / "ws2"))
    from specforge.core import db

    db._local.__dict__.clear()
    from specforge.api.app import create_app

    with TestClient(create_app()) as b:
        r = b.post("/api/backup/import", files={"file": ("b.zip", data, "application/zip")}, data={"items": "models"})
        assert r.status_code == 200, r.text
        st = b.get("/api/offline").json()
        assert st["downloaded"] is not None and st["offline"] is True
        assert (models.default_folder() / "layout" / "w.bin").read_bytes() == b"weights"
        assert st["loading_from"] == str(models.default_folder())
