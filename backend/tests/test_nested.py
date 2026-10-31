"""Nested tables (ingest/nested.py) on real pages of NVMe Base 2.4: Identify Controller (pp. 366-380, a bit table found as a
table of its own for CMIC, in extra columns over four pages for CTRATT, value tables inside bits), SMART / Health
Information (pp. 247-248) and Command Dword 0 (pp. 165-166, OPC -> DTD values two levels down)."""
from pathlib import Path

import pytest

from specforge.ingest import nested

BASE = Path(__file__).resolve().parents[2] / "docs/specifications/NVM-Express-Base-Specification-Revision-2.4-Ratified-2026.07.31.pdf"
pytestmark = pytest.mark.skipif(not BASE.exists(), reason="the NVMe Base 2.4 PDF is not in this checkout")
PAGES = [(165, 166), (247, 248), (366, 380)]


def pg(spec_page: int) -> int:
    """The page of the excerpt that is that page of the spec."""
    n = 0
    for a, b in PAGES:
        if a <= spec_page <= b:
            return n + spec_page - a + 1
        n += b - a + 1
    raise ValueError(spec_page)


@pytest.fixture(scope="module")
def excerpt(tmp_path_factory) -> Path:
    import pymupdf

    src = pymupdf.open(str(BASE))
    out = pymupdf.open()
    for a, b in PAGES:
        out.insert_pdf(src, from_page=a - 1, to_page=b - 1)
    p = tmp_path_factory.mktemp("nested") / "base-2.4-excerpt.pdf"
    out.save(str(p))
    return p


@pytest.fixture(scope="module")
def found(excerpt) -> dict[str, nested.Nested]:
    return {n.label: n for n in nested.find_nested(str(excerpt))}


def test_bit_and_value_tables_are_found_with_their_parents(found):
    cmic = found["338.76"]                                       # a table of its own inside the CMIC row
    assert [r[0] for r in cmic.rows] == ["7:4", "3", "2", "1", "0"] and cmic.header == ["Bits", "Description"]
    assert cmic.field_text.startswith("Controller Multi-Path I/O and Namespace Sharing Capabilities (CMIC):")
    ctratt = found["338.99:96"]                                  # extra columns of the parent, over four pages
    assert [r[0] for r in ctratt.rows][:3] == ["31:25", "24:23", "22"] and ctratt.rows[-1][0] == "0" and len(ctratt.rows) == 25
    assert ctratt.pages == [pg(370), pg(371), pg(372), pg(373)]
    assert ctratt.field_text == "Controller Attributes (CTRATT): This field indicates attributes of the controller."
    # text read from the page, not from cells: a wrapped line's last letters are not lost to a column of their own, and a
    # description going on over a page break stays with its bit
    by_bit = {r[0]: r[1] for r in ctratt.rows}
    assert "then the controller supports SQ Associations" in by_bit["8"]
    assert by_bit["14"].endswith("then the controller does not support the Delete NVM Set operation.")
    assert by_bit["1"].endswith("Refer to section 5.2.30.1.11.") and "(HIDS)" not in by_bit["1"]
    values = found["338.99:96.24:23"]                            # a value table inside bit 24:23
    assert [r[0] for r in values.rows] == ["00b", "01b", "10b", "11b"] and values.header == ["Value", "Definition"]
    assert found["338.95:92"].pages == [pg(368), pg(369), pg(370)]           # OAES goes on over pages too
    assert [r[0] for r in found["213.00"].rows][:2] == ["7", "6"]    # SMART Critical Warning
    assert [r[0] for r in found["92.07:00"].rows] == ["07:02", "01:00"]       # Command Dword 0 OPC ...
    assert [r[0] for r in found["92.07:00.01:00"].rows] == ["00b", "01b", "10b", "11b"]   # ... and DTD's values


def test_the_parser_puts_nested_tables_under_their_parent_rows(excerpt):
    from specforge.ingest.pdf import parse_pdf

    spec = parse_pdf(str(excerpt), "nvme-base", "2.4")
    by = {t.label: t for t in spec.tables}
    st = spec.stats["nested_tables"]
    assert st["nested"] >= 20 and st["unplaced"] == 0
    child = by["338.99:96"]
    parent = by[child.parent_table]
    row = parent.rows[child.parent_cell[0]]
    assert row[0] == "99:96" and child.caption == "Controller Attributes (CTRATT): bits"
    assert row[child.parent_cell[1]] == "Controller Attributes (CTRATT): This field indicates attributes of the controller."
    values = by["338.99:96.24:23"]
    assert values.parent_table == "338.99:96" and child.rows[values.parent_cell[0]][0] == "24:23"
    assert "Value Definition" not in child.rows[values.parent_cell[0]][1]       # the values are not flattened into the bit
    # no copies left: neither nested rows in the parent tables nor uncaptioned pieces holding them
    for t in spec.tables:
        if t.label.split("#")[0] == "338" and not t.parent_table:
            assert not [r for r in t.rows if not r[0] and nested.KEY.match(nested._first(r))], t.label
    assert not [t.label for t in spec.tables if not t.captioned and t.extractor != "pymupdf-nested"]
    assert any(r.table == "338.99:96" for r in spec.requirements)       # normative statements are read from them too


def test_the_markdown_prints_each_nested_table_after_its_parent(excerpt):
    from specforge.convert import convert_file

    md = convert_file(str(excerpt), engine="builtin").markdown
    row = "| 99:96 | M |  | M |  | 4 O |  | Controller Attributes (CTRATT): This field indicates attributes of the controller. |"
    bits = "**Figure 338 – Bytes 99:96, Controller Attributes (CTRATT): bits**"
    vals = "**Figure 338 – Bytes 99:96, Controller Attributes (CTRATT), bits 24:23, Idle I/O Exit Latency Limit Support and Scope (IIELLSS): values**"
    assert row in md and bits in md and vals in md
    assert md.index(row) < md.index(bits) < md.index("| 31:25 | Reserved |") < md.index(vals) < md.index("| 00b | Not Supported:")
    assert md.count(bits) == 1 and "Bits Description 31:25" not in md


def test_mcp_answers_carry_bits_and_values(excerpt, conn, ws):
    from specforge.ingest import store
    from specforge.specquery import SpecLibrary

    d = store.register_document(conn, excerpt, "nvme-base", "2.4")
    store.run_parse(conn, d["id"], workers=1, activate=True, engine="builtin")
    lib = SpecLibrary(conn_factory=lambda: conn)
    md = lib.structure("Identify Controller", field="IIELLSS", spec="base")
    assert "| 99:96 |" in md and "↳ 24:23 · Idle I/O Exit Latency Limit Support and Scope (IIELLSS)" in md
    assert "↳↳ 00b · Not Supported" in md and "↳↳ 11b · Reserved" in md
    bit = md.data["fields"][0]["bits"][0]
    assert bit["bits"] == "24:23" and [v["value"] for v in bit["values"]] == ["00b", "01b", "10b", "11b"]
    whole = lib.structure("Identify Controller", field="CTRATT", spec="base")
    assert [b["bits"] for b in whole.data["fields"][0]["bits"]][:3] == ["31:25", "24:23", "22"]
    assert "338.99:96" not in lib.ir(lib.resolve_doc("base")[0])["tables"]     # not a figure of its own
