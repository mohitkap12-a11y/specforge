"""Shapes from the real NVMe Base 2.4 / MI 2.2 / NVM CS 1.3 PDFs that made the completeness audit fail."""
from specforge.ingest.audit import audit_spec
from specforge.ingest.ir import ParsedSpec, Table
from specforge.ingest.normalize import extract_references, nospace_len, parse_byte_range
from specforge.ingest.pdf import assemble


def _line(text, y, bold=False, x0=72.0, block=None):
    return {"text": text, "x0": x0, "y0": y, "x1": x0 + 6 * len(text), "y1": y + 10, "block": block or int(y), "size": 10.0, "bold": bold}


def _page(n, lines, tables=()):
    raw = sum(nospace_len(l["text"]) for l in lines)
    return {"page": n, "height": 792.0, "width": 612.0, "lines": lines, "tables": list(tables), "raw_chars": raw}


def _table(bbox, rows, quality=0.9):
    return {"bbox": bbox, "rows": rows, "extractor": "pymupdf", "quality": quality}


def _audit(pages, toc):
    spec = assemble("x.pdf", pages, "nvme-base", "2.4", "t", "pdf", toc)
    issues, summary = audit_spec(spec)
    return spec, issues, summary


def test_a_deep_heading_printed_without_its_number_is_found():
    pages = [_page(1, [_line("1.4.1", 100, True), _line("Keywords", 100, True, x0=110),
                       _line("mandatory", 130, True), _line("A keyword indicating items to be implemented.", 150)])]
    spec, issues, _ = _audit(pages, [[3, "1.4.1 Keywords", 1], [4, "1.4.1.1 mandatory", 1]])
    assert not [i for i in issues if i["kind"] == "heading_not_found"]
    sec = next(s for s in spec.sections if s.number == "1.4.1.1")
    assert sec.paragraphs == ["A keyword indicating items to be implemented."]


def test_a_bookmark_that_points_at_the_previous_page_still_finds_its_heading():
    pages = [_page(1, [_line("Contents text.", 100)]), _page(2, [_line("Table of Figures", 80, True), _line("Figure list.", 100)])]
    _, issues, _ = _audit(pages, [[1, "Table of Figures", 1]])
    assert not [i for i in issues if i["kind"] == "heading_not_found"]


def test_nested_tables_and_repeated_link_text_are_not_counted_twice():
    outer = _table([72, 100, 540, 300], [["Bytes", "Description"], ["03:00", "Status: Bits Description 15:01 Status 00 Reserved"]])
    inner = _table([150, 150, 500, 250], [["Bits", "Description"], ["15:01", "Status"], ["00", "Reserved"]])
    lines = [_line("Figure 1: Response", 80), _line("Bytes", 110), _line("Description", 110, x0=150), _line("03:00", 130),
             _line("Status:", 130, x0=150), _line("Bits", 160, x0=160), _line("Description", 160, x0=200), _line("15:01", 180, x0=160),
             _line("Status", 180, x0=200), _line("00", 200, x0=160), _line("Reserved", 200, x0=200),
             _line("Refer to Figure 1Figure 1 for the response.", 400)]
    spec, issues, summary = _audit([_page(1, lines, [outer, inner])], [])
    assert summary["checks"]["text_accounting"]["pass"], summary["checks"]["text_accounting"]
    assert spec.stats["char_accounting"]["link_doubles"] == nospace_len("Figure 1")


def test_a_doubled_link_wrapped_over_two_lines_is_collapsed():
    lines = [_line("1 Intro", 80, True), _line("The status is listed (refer to Figure 638Figure", 100, block=1), _line("638).", 112, block=1)]
    spec, _, _ = _audit([_page(1, lines)], [[1, "1 Intro", 1]])
    assert spec.sections[1].paragraphs == ["The status is listed (refer to Figure 638)."]


def test_a_caption_drawn_inside_the_table_frame_labels_the_table():
    t = _table([72, 100, 540, 300], [["", "Figure 201: Completion Queue Entry Dword 0", "", ""], ["", "Bits", "", "Description"],
                                     ["31:03", "", "", "Reserved"], ["02:00", "", "", "Flags"]])
    spec, issues, _ = _audit([_page(1, [_line("See Figure 201.", 80)], [t])], [])
    fig = next(x for x in spec.tables if x.label == "201")
    assert fig.caption == "Completion Queue Entry Dword 0" and fig.rows[0][0] == "31:03"
    assert not [i for i in issues if i["kind"] in ("figure_reference_missing", "uncaptioned_table")]


def test_two_tables_drawn_as_one_grid_are_split_at_the_second_caption():
    t = _table([72, 100, 540, 500], [["Command", "Opcode"], ["Flush", "00h"], ["Figure 29: Fabrics Commands", ""],
                                     ["Command", "Opcode"], ["Connect", "01h"]])
    lines = [_line("Figure 28: Admin Commands", 80), _line("Figure 29: Fabrics Commands", 300, x0=180), _line("See Figure 29.", 600)]
    spec, issues, _ = _audit([_page(1, lines, [t])], [])
    figs = {x.label: x for x in spec.tables}
    assert figs["28"].rows == [["Flush", "00h"]] and figs["29"].rows == [["Connect", "01h"]]
    assert not [i for i in issues if i["kind"] == "figure_reference_missing"]


def _schema(caption, header, rows):
    spec = ParsedSpec(family="nvme-base", version="2.4", title="t", fmt="pdf", pages=1, sections=[], requirements=[], references=[],
                      excluded=[], tables=[Table(label="1", caption=caption, header=header, rows=rows, page_from=1, page_to=1)])
    return audit_spec(spec)[1]["checks"]["table_schema"]


def test_label_symbolic_and_row_spanned_keys_are_not_byte_ranges():
    rows = [["Header", "", ""], ["01:00", "", "Number of Identifiers"], ["Identifier List", "", ""], ["03:02", "", "Identifier 0"],
            ["…", "", "…"], ["(NUM*2+1): (NUM*2)", "", "Identifier NUM-1"], ["", "", "nested bit row"], ["1791:102 4", "", "Reserved"]]
    assert _schema("List Format", ["Bytes", "", "Description"], rows)["pass"]
    assert parse_byte_range("1791:102 4") == (1791, 1024)


def test_values_may_repeat_in_separate_groups_of_one_table():
    rows = [["0", "", "32"], ["32", "", "32"], ["", "32b Guard Protection Information", ""], ["0", "", "48"], ["48", "", "48"]]
    assert _schema("Storage Tag Sizes", ["STS Value", "Guard", "Bit Size"], rows)["pass"]
    assert not _schema("Storage Tag Sizes", ["STS Value", "Guard", "Bit Size"], [["0", "", "32"], ["0", "", "48"]])["pass"]


def test_opcode_duplicates_are_checked_on_the_combined_opcode_column():
    rows = [["0000 00b", "00b", "00h", "Delete I/O Submission Queue"], ["0000 00b", "01b", "01h", "Create I/O Submission Queue"]]
    assert _schema("Opcodes for Admin Commands", ["Opcode by Field", "", "Combined Opcode", "Command"], rows)["pass"]


def test_a_bit_range_interleaved_after_figure_is_not_a_figure_reference():
    assert [r.label for r in extract_references("the identifier of the AE (refer to Figure 07:00 63).", "nvme-mi")] == []
    assert [r.label for r in extract_references("Refer to Figure 63: the AE list.", "nvme-mi")] == ["63"]
