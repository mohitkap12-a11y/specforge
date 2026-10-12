import warnings

import pytest

warnings.filterwarnings("ignore")


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory):
    from specforge.devtools.synth import make_command_db, make_demo_specs

    d = tmp_path_factory.mktemp("demo")
    make_demo_specs(d)
    make_command_db(d / "cmds-a.db", "A")
    make_command_db(d / "cmds-b.db", "B")
    return d


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECFORGE_HOME", str(tmp_path / "ws"))
    from specforge.core import db

    db._local.__dict__.clear()
    return tmp_path / "ws"


@pytest.fixture()
def conn(ws):
    from specforge.core.db import connect

    c = connect()
    yield c
    c.close()


@pytest.fixture()
def parsed_set(conn, demo_dir):
    """Base 2.4 + NVM CS 1.3 + MI 2.0 parsed and grouped in spec set 1, catalog + templates built."""
    from specforge import catalog, scenarios
    from specforge.ingest import store

    ids = []
    for name in ("nvme-base-2.4.pdf", "nvme-nvm-cs-1.3.pdf", "nvme-mi-2.0.pdf"):
        d = store.register_document(conn, demo_dir / name)
        store.run_parse(conn, d["id"], workers=1)
        ids.append(d["id"])
    conn.execute("INSERT INTO spec_set(name) VALUES('demo')")
    for i in ids:
        conn.execute("INSERT INTO spec_set_member VALUES(1,?)", (i,))
    conn.commit()
    catalog.build_catalog(conn, 1)
    scenarios.generate_templates(conn, 1)
    return 1
