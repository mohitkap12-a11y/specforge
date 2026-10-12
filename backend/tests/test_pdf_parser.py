from specforge.devtools.synth import base_spec, write_pdf
from specforge.ingest.audit import audit_spec
from specforge.ingest.pdf import parse_pdf


def table(spec, label):
    return next(t for t in spec.tables if t.label == label)


def test_identity_and_section_tree(demo_dir):
    s = parse_pdf(str(demo_dir / "nvme-base-2.4.pdf"), workers=1)
    assert (s.family, s.version, s.pages) == ("nvme-base", "2.4", 6)
    nums = [x.number for x in s.sections]
    assert nums == ["front", "1", "5", "5.1", "5.2", "5.2.1", "5.3", "5.4", "5.5"]
    sec = {x.number: x for x in s.sections}
    assert sec["5.2.1"].parent == "5.2" and sec["5.2"].parent == "5" and sec["5"].parent is None
    assert sec["5.2"].title == "Identify command"
    # heading lines are not duplicated into body text
    assert not any("5.2 Identify command" in p for p in sec["5.2"].paragraphs)


def test_tables_captions_and_cells(demo_dir):
    s = parse_pdf(str(demo_dir / "nvme-base-2.4.pdf"), workers=1)
    assert [t.label for t in s.tables] == ["100", "101", "102", "103", "104", "120", "121", "130", "131", "140", "141"]
    t = table(s, "101")
    assert t.caption == "Identify – Command Dword 10" and t.header == ["Bits", "Description"]
    assert [r[0] for r in t.rows] == ["31:16", "15:08", "07:00"]
    assert t.rows[1][1] == "Reserved"
    assert t.section == "5.2"
    # cell text keeps the full sentence (wrapped lines joined)
    assert "Refer to Figure 103" in t.rows[2][1]


def test_multi_page_table_is_stitched(demo_dir):
    s = parse_pdf(str(demo_dir / "nvme-base-2.4.pdf"), workers=1)
    t = table(s, "121")
    assert t.page_to > t.page_from
    ids = [r[0] for r in t.rows]
    assert ids[0] == "01h" and ids[-1] == "3Dh" and len(ids) == 61 and len(set(ids)) == 61  # no dropped / duplicated rows
    assert all(r[0] != "Log Page Identifier" for r in t.rows)  # repeated header rows removed
    assert t.continued_pages >= 1


def test_caption_at_page_bottom_binds_to_table_on_next_page(demo_dir):
    s = parse_pdf(str(demo_dir / "nvme-nvm-cs-1.3.pdf"), workers=1)
    t = table(s, "40")
    assert t.caption == "Write – Command Dword 12" and len(t.rows) == 4 and t.page_from == 3
    # and the preceding table must not have swallowed it
    assert len(table(s, "31").rows) == 5


def test_header_footer_excluded_and_text_accounted(demo_dir):
    s = parse_pdf(str(demo_dir / "nvme-base-2.4.pdf"), workers=1)
    cats = {e.text for e in s.excluded}
    assert "nvme-base 2.4 synthetic" in cats and any(c.startswith("Page ") for c in cats)
    assert s.raw_char_count == s.accounted_char_count > 0
    assert all("Copyright synthetic" not in p for sec in s.sections for p in sec.paragraphs)


def test_requirements_and_references(demo_dir):
    s = parse_pdf(str(demo_dir / "nvme-base-2.4.pdf"), workers=1)
    strengths = {r.strength for r in s.requirements}
    assert {"mandatory", "recommended", "optional"} <= strengths
    r = next(r for r in s.requirements if "return zeros" in r.text)
    assert r.section == "5.2.1"
    cross = [x for x in s.references if x.target_family == "nvme-nvm-cs"]
    assert cross and cross[0].target_label == "3.1" and cross[0].kind == "section"
    # references inside a table cell are attributed to the table
    assert any(x.src_key == "fig:101" and x.target_label == "103" for x in s.references)


def test_audit_clean_on_good_spec(demo_dir):
    for name in ("nvme-base-2.4.pdf", "nvme-nvm-cs-1.3.pdf", "nvme-mi-2.0.pdf"):
        s = parse_pdf(str(demo_dir / name), workers=1)
        issues, summary = audit_spec(s)
        errs = [i for i in issues if i["severity"] == "error"]
        assert not errs, errs
        assert summary["coverage_ready"] and summary["score"] == 1.0


def test_audit_detects_defects(demo_dir):
    s = parse_pdf(str(demo_dir / "nvme-base-2.4.pdf"), workers=1)
    # overlapping bit ranges
    t = table(s, "101")
    t.rows.append(["09:04", "Overlapping row"])
    # dangling figure reference
    s.references.append(type(s.references[0])("Figure 999", "figure", None, "999", "sec:5.2", 2))
    # lost text
    s.accounted_char_count -= 500
    issues, summary = audit_spec(s)
    kinds = {i["kind"] for i in issues if i["severity"] == "error"}
    assert {"bit_range_overlap", "figure_reference_missing", "text_accounting"} <= kinds
    assert not summary["coverage_ready"]


def test_parallel_extraction_matches_serial(tmp_path):
    p = tmp_path / "big.pdf"
    write_pdf(p, base_spec(lid_rows=700), header="nvme-base 2.4 synthetic")
    a = parse_pdf(str(p), workers=1)
    b = parse_pdf(str(p), workers=3)
    assert a.pages >= 12
    assert [(t.label, t.rows, t.page_from, t.page_to) for t in a.tables] == [(t.label, t.rows, t.page_from, t.page_to) for t in b.tables]
    assert [(s.number, s.paragraphs) for s in a.sections] == [(s.number, s.paragraphs) for s in b.sections]
    assert len(table(a, "121").rows) == 699 and a.raw_char_count == a.accounted_char_count


def test_pdf_without_bookmarks_falls_back_to_bold_numbered_headings(tmp_path):
    import pymupdf

    p = tmp_path / "nobm.pdf"
    write_pdf(p, base_spec(), header="x")
    doc = pymupdf.open(str(p))
    doc.set_toc([])
    doc.save(str(tmp_path / "nobm2.pdf"))
    s = parse_pdf(str(tmp_path / "nobm2.pdf"), workers=1)
    nums = {x.number for x in s.sections}
    assert {"5.2", "5.3", "5.4"} <= nums
