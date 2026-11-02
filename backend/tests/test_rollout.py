"""Bringing what is already on a server up to date after the parser or a converter changes: outdated To Markdown files are not
reused and an admin re-converts them in place; specs parsed by an older parser are reparsed in one go (not activated)."""
import io
import json
import time

from fastapi.testclient import TestClient


def _wait(client, job):
    for _ in range(600):
        j = client.get(f"/api/jobs/{job}").json()
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _age(c, job: int) -> None:
    """Make a conversion look like one an older converter made (before versions were recorded)."""
    res = json.loads(c.execute("SELECT result_json FROM job WHERE id=?", (job,)).fetchone()[0])
    res.pop("converter_version", None)
    c.execute("UPDATE job SET result_json=? WHERE id=?", (json.dumps(res), job))
    c.commit()


def test_outdated_conversions_are_not_reused_and_are_converted_again(ws, demo_dir):
    from specforge.api.app import create_app
    from specforge.convert import CONVERTER_VERSION
    from specforge.core.db import connect

    pdf = (demo_dir / "nvme-mi-2.0.pdf").read_bytes()
    with TestClient(create_app()) as client:
        first = client.post("/api/convert/markdown/jobs?engine=builtin", files={"file": ("mi.pdf", io.BytesIO(pdf))}).json()["job"]
        assert _wait(client, first)["state"] == "done"
        entry = next(e for e in client.get("/api/convert/markdown/jobs").json() if e["job"] == first)
        assert entry["outdated"] is False and entry["result"]["converter_version"] == CONVERTER_VERSION["builtin"]
        assert client.post("/api/convert/markdown/reconvert-outdated").json() == {"job": None, "count": 0, "no_source": 0}

        c = connect()
        _age(c, first)
        folder = json.loads(c.execute("SELECT result_json FROM job WHERE id=?", (first,)).fetchone()[0])["dir"]
        c.execute("INSERT INTO mcp_lib_index(dir, filename, version, entries, seconds) VALUES(?, 'mi.pdf', '3', 10, 0.1)", (folder,))
        c.commit()
        assert next(e for e in client.get("/api/convert/markdown/jobs").json() if e["job"] == first)["outdated"] is True

        r = client.post("/api/convert/markdown/reconvert-outdated").json()
        assert r["count"] == 1 and r["no_source"] == 0
        assert _wait(client, r["job"])["state"] == "done"
        entry = next(e for e in client.get("/api/convert/markdown/jobs").json() if e["job"] == first)
        assert entry["outdated"] is False and entry["result"]["converter_version"] == CONVERTER_VERSION["builtin"]
        assert entry["result"]["filename"] == "mi.md"                                         # same name and place
        assert c.execute("SELECT COUNT(*) FROM mcp_lib_index WHERE dir=?", (folder,)).fetchone()[0] == 0   # indexed again

        # an outdated file is converted again when it is uploaded again, not handed back
        assert client.post("/api/convert/markdown/jobs?engine=builtin", files={"file": ("mi.pdf", io.BytesIO(pdf))}).json() == \
            {"job": first, "reused": True}
        _age(c, first)
        again = client.post("/api/convert/markdown/jobs?engine=builtin", files={"file": ("mi.pdf", io.BytesIO(pdf))}).json()
        assert "reused" not in again and again["job"] != first
        _wait(client, again["job"])

        # without its original (converted before originals were kept) a file is left as it is and counted
        for f in (ws / "conversions" / folder).glob("source.*"):
            f.unlink()
        assert client.post("/api/convert/markdown/reconvert-outdated").json() == {"job": None, "count": 0, "no_source": 1}
        c.close()


def test_specs_parsed_by_an_older_parser_are_reparsed_without_activating(ws, demo_dir):
    from specforge.api.app import create_app
    from specforge.core.config import PARSER_VERSION
    from specforge.core.db import connect
    from specforge.ingest import store

    c = connect()
    d = store.register_document(c, demo_dir / "nvme-mi-2.0.pdf")
    old = store.run_parse(c, d["id"], workers=1, activate=True, engine="builtin")
    c.execute("UPDATE parse_run SET parser_version='3' WHERE id=?", (old,))
    c.commit()
    with TestClient(create_app()) as client:
        assert client.post("/api/specs/reparse-outdated?engine=nope").status_code == 400
        r = client.post("/api/specs/reparse-outdated").json()                # the engine its active run used: built-in
        assert r["count"] == 1 and r["jobs"][0]["document_id"] == d["id"]
        assert _wait(client, r["jobs"][0]["job"])["state"] == "done"
    runs = c.execute("SELECT id, parser_version, active, stats_json FROM parse_run WHERE document_id=? ORDER BY id", (d["id"],)).fetchall()
    assert [(x[1], x[2]) for x in runs] == [("3", 1), (PARSER_VERSION, 0)]    # the new run waits to be checked and activated
    assert json.loads(runs[1][3])["engine"] == "builtin"
    assert store.reparse_available(c) == [d["id"]]
    c.close()
