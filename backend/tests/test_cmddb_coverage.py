import json
import sqlite3
import struct

import pytest

from specforge import cmddb, coverage
from specforge.core.db import rows
from specforge.devtools.synth import mi_admin_msg, sqe


def run_cov(conn, demo_dir, name="cmds-a.db"):
    sid = cmddb.register_source(conn, str(demo_dir / name))
    cmddb.import_source(conn, sid)
    return sid, coverage.run_coverage(conn, 1, sid)


def state(conn, run, key):
    return conn.execute("SELECT state FROM item_result WHERE run_id=? AND item_key=?", (run, key)).fetchone()[0]


def test_sqe_decode_bit_exact():
    d = cmddb.decode_sqe(sqe(0x06, nsid=7, cid=0xBEEF, cdw10=0x00010001, fuse=1, psdt=2))
    assert d["opcode"] == 6 and d["nsid"] == 7 and d["extra"] == {"fuse": 1, "psdt": 2, "cid": 0xBEEF}
    assert d["dwords"]["10"] == 0x00010001
    with pytest.raises(ValueError):
        cmddb.decode_sqe(b"\x00" * 63)


def test_mi_decode_and_signature_ignores_cid_and_pointers():
    m = cmddb.decode_mi(mi_admin_msg(0x06, nsid=3, cdw10=1), cmddb.merged_layout(None))
    assert (m["interface"], m["queue"], m["opcode"], m["nsid"], m["dwords"]["10"]) == ("mi", "admin", 6, 3, 1)
    a = cmddb.decode_sqe(sqe(2, cid=1, prp1=0x1000, cdw12=7))
    b = cmddb.decode_sqe(sqe(2, cid=2, prp1=0x9000, cdw12=7))
    c = cmddb.decode_sqe(sqe(2, cid=1, prp1=0x1000, cdw12=8))
    sig = lambda d: cmddb.signature("pcie", "io", "nvm", d["opcode"], d["dwords"])
    assert sig(a) == sig(b) != sig(c)


def test_layouts_a_and_b_decode_identically(conn, demo_dir):
    sa = cmddb.register_source(conn, str(demo_dir / "cmds-a.db"))
    sb = cmddb.register_source(conn, str(demo_dir / "cmds-b.db"))
    assert "cdw" in json.loads(rows(conn, "SELECT mapping_json FROM cmd_source WHERE id=?", (sb,))[0]["mapping_json"])
    ra, rb = cmddb.import_source(conn, sa), cmddb.import_source(conn, sb)
    assert ra["undecodable"] == 0 and ra["rows"] == 219 and ra["signatures"] == 19
    # layout B has no MI rows; every PCIe signature must be identical to layout A's
    pcie = lambda s: {r[0] for r in conn.execute("SELECT signature FROM cmd_signature WHERE source_id=? AND interface='pcie'", (s,))}
    assert pcie(sa) == pcie(sb) and rb["rows"] == 215


def test_bad_rows_are_counted_not_dropped_silently(conn, tmp_path):
    p = tmp_path / "bad.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, test_name TEXT, seq INTEGER, interface TEXT, queue TEXT, sqe BLOB)")
    con.execute("INSERT INTO commands VALUES(1,'t',1,'pcie','admin',?)", (sqe(6),))
    con.execute("INSERT INTO commands VALUES(2,'t',2,'pcie','admin',?)", (b"short",))
    con.commit(); con.close()
    sid = cmddb.register_source(conn, str(p))
    r = cmddb.import_source(conn, sid)
    assert (r["rows"], r["decoded"], r["undecodable"]) == (2, 1, 1) and r["errors"][0]["row"] == 2


def test_coverage_matches_planted_commands(conn, parsed_set, demo_dir):
    sid, run = run_cov(conn, demo_dir)
    p = "cmd:admin:06/dw10[07:00]"
    assert [state(conn, run, f"{p}={v}") for v in ("00h", "01h", "02h", "03h", "10h")] == ["covered", "covered", "covered", "not_covered", "not_covered"]
    assert state(conn, run, "cmd:admin:06/dw10[15:08]:rsvd") == "stimulus_covered"      # planted reserved bit
    assert state(conn, run, "cmd:admin:06/dw11[23:16]:rsvd") == "not_covered"
    assert state(conn, run, "cmd:admin:06/mi-tunnel") == "covered"
    assert state(conn, run, "cmd:io:nvm:02/dw12[31:31]=01h") == "covered"                 # Read with LR
    assert state(conn, run, "cmd:io:nvm:01/dw12[31:31]=01h") == "not_covered"             # Write never with LR
    assert state(conn, run, "cmd:admin:09/dw10[31:31]=01h") == "covered"                  # Set Features SV=1
    assert state(conn, run, "cmd:mi:00/nmd0[31:24]=03h") == "covered"
    assert state(conn, run, "cmd:admin:15") == "not_covered"
    summary = json.loads(rows(conn, "SELECT summary_json FROM coverage_run WHERE id=?", (run,))[0]["summary_json"])
    assert summary["unknown_commands"] == [{"interface": "pcie", "queue": "admin", "opcode": 0xC1, "commands": 1}]


def test_coverage_is_deterministic(conn, parsed_set, demo_dir):
    sid, r1 = run_cov(conn, demo_dir)
    r2 = coverage.run_coverage(conn, 1, sid)
    dump = lambda r: rows(conn, "SELECT item_key,state,covered,total FROM item_result WHERE run_id=? ORDER BY item_key", (r,))
    assert dump(r1) == dump(r2)
    d = coverage.diff_runs(conn, r1, r2)
    assert d["gained"] == d["lost"] == []


def test_matcher_agrees_with_independent_oracle(conn, parsed_set, demo_dir):
    """Brute-force every approved single-step template scenario over the raw SQE bytes."""
    sid, run = run_cov(conn, demo_dir)
    raw = [bytes(r[0]) for r in sqlite3.connect(demo_dir / "cmds-a.db").execute("SELECT sqe FROM commands WHERE sqe IS NOT NULL")]
    for s in rows(conn, "SELECT * FROM scenario WHERE origin='template' AND kind IN ('command','value','boundary','reserved')"):
        step = json.loads(s["predicate_json"])["steps"][0]
        if step["interface"] != "pcie":
            continue
        hits = 0
        for b in raw:
            dw = struct.unpack("<16I", b)
            if (dw[0] & 0xFF) != step["opcode"]:
                continue
            ok = True
            for f in step["fields"]:
                v = (dw[f["dw"]] >> f["lo"]) & ((1 << (f["hi"] - f["lo"] + 1)) - 1)
                ok &= {"eq": lambda: v == f["value"], "ne": lambda: v != f["value"],
                       "range": lambda: f["value"]["lo"] <= v <= f["value"]["hi"]}[f["op"]]()
            hits += ok
        # admin vs io share opcodes numerically (e.g. 0x01/0x02): restrict oracle to the right queue
        want_q = step["queue"]
        rows_q = [bytes(r[0]) for r in sqlite3.connect(demo_dir / "cmds-a.db").execute("SELECT sqe FROM commands WHERE sqe IS NOT NULL AND queue=?", (want_q,))]
        hits = 0
        for b in rows_q:
            dw = struct.unpack("<16I", b)
            if (dw[0] & 0xFF) != step["opcode"]:
                continue
            ok = True
            for f in step["fields"]:
                v = (dw[f["dw"]] >> f["lo"]) & ((1 << (f["hi"] - f["lo"] + 1)) - 1)
                ok &= {"eq": lambda: v == f["value"], "ne": lambda: v != f["value"],
                       "range": lambda: f["value"]["lo"] <= v <= f["value"]["hi"]}[f["op"]]()
            hits += ok
        got = conn.execute("SELECT match_count FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, s["id"])).fetchone()[0]
        assert (got > 0) == (hits > 0), s["sid"]
        if hits:
            assert got >= 1


def seq_scenario(conn, steps, **kw):
    pred = {"scope": "test", "order": "strict", "steps": steps, **kw}
    n = conn.execute("SELECT COUNT(*) FROM scenario WHERE origin='manual'").fetchone()[0]
    cur = conn.execute("INSERT INTO scenario(set_id,sid,origin,kind,title,observability,predicate_json,state) VALUES(1,?, 'manual','seq','t','submission',?, 'approved')",
                       (f"M:{n}", json.dumps(pred)))
    conn.commit()
    return cur.lastrowid


def step(opcode, fid=None, capture=None, var=None):
    f = []
    if fid is not None:
        f.append({"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": fid})
    if var:
        f.append({"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": {"var": var}})
    s = {"interface": "pcie", "queue": "admin", "cs": None, "opcode": opcode, "fields": f}
    if capture:
        s["capture"] = {capture: {"dw": 10, "hi": 7, "lo": 0}}
    return s


def test_sequence_with_variable_binding_and_order(conn, parsed_set, demo_dir):
    sid = cmddb.register_source(conn, str(demo_dir / "cmds-a.db")); cmddb.import_source(conn, sid)
    set_then_get = seq_scenario(conn, [step(0x09, capture="fid"), step(0x0A, var="fid")])
    get_then_set = seq_scenario(conn, [step(0x0A, capture="fid"), step(0x09, var="fid")])
    within1 = seq_scenario(conn, [step(0x09, fid=7), step(0x0A, fid=7)], within=1)
    any_order = seq_scenario(conn, [step(0x0A, fid=7), step(0x09, fid=7)], order="any")
    strict_reversed = seq_scenario(conn, [step(0x0A, fid=7), step(0x09, fid=7)])
    set_then_identify_in_test = seq_scenario(conn, [step(0x09, fid=7), step(0x06)])
    set_then_identify_global = seq_scenario(conn, [step(0x09, fid=7), step(0x06)], scope="global")
    run = coverage.run_coverage(conn, 1, sid)
    res = lambda i: tuple(conn.execute("SELECT state,match_count FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, i)).fetchone())
    # test_features: seq1 Set(FID7) seq2 Get(FID7) seq3 Get(FID4) seq4 Set(FID1)
    assert res(set_then_get) == ("matched", 1)           # Set FID7 -> Get FID7; the later Set FID1 has no Get after it
    assert res(get_then_set) == ("not_matched", 0)       # no Set follows a Get with the same FID
    assert res(within1) == ("matched", 1)
    assert res(any_order)[0] == "matched"
    assert res(strict_reversed)[0] == "not_matched"      # Get FID7 never precedes a Set FID7
    assert res(set_then_identify_in_test)[0] == "not_matched"   # Identify happens in another test
    assert res(set_then_identify_global)[0] == "matched"        # ...but exists later in a global scan (test_features < test_identify)


def test_draft_scenarios_never_count(conn, parsed_set, demo_dir):
    sid = cmddb.register_source(conn, str(demo_dir / "cmds-a.db")); cmddb.import_source(conn, sid)
    conn.execute("UPDATE scenario SET state='draft' WHERE sid=?", ("T:cmd:admin:06",))
    conn.commit()
    run = coverage.run_coverage(conn, 1, sid, analyze=False)       # spec-driven scenarios only
    assert state(conn, run, "cmd:admin:06") == "needs_review"
    run = coverage.run_coverage(conn, 1, sid)                      # the log itself shows Identify was executed
    assert state(conn, run, "cmd:admin:06") == "covered"
