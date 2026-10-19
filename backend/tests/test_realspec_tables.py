"""Table shapes found in the real NVMe PDFs (merged cells, footnotes, wide fields, completion layouts), as small fixtures."""
from test_macroscope_fixes import T, build

from specforge import catalog
from specforge.catalog import _opcode_cols
from specforge.core.db import jloads, rows
from specforge.ingest.normalize import compact_table


def test_compact_table_drops_only_always_empty_columns():
    head, data = compact_table(["", "Bits", "", "", "Description", ""], [["31:16", "", "", "Alpha", "", ""], ["15:08", "", "", "Reserved", "", ""]])
    assert head == ["Bits", "Description"] and data == [["31:16", "Alpha"], ["15:08", "Reserved"]]
    keep = [["00h", "M", "Foo"], ["01h", "", "Bar"]]                      # a genuinely blank cell in a used column stays
    assert compact_table(["Opcode", "O/M", "Command"], keep) == (["Opcode", "O/M", "Command"], keep)


def test_opcode_columns_are_found_by_content_not_header_names():
    rows_ = [["Function", "", "Opcode", "Data", "Command", "Reference"], ["", "(07:02)", "", "", "", ""],
             ["0000 00b", "", "00h", "No", "Delete I/O Submission Queue", "5.3.4"], ["0000 01b", "", "05h", "No", "3 Create I/O Completion Queue", "5.3.1"],
             ["11xx xxb", "", "C0h to FFh", "", "Vendor specific", ""], ["Notes: 1. Opcodes not listed are reserved.", "", "", "", "", ""]]
    assert _opcode_cols(rows_) == (2, 4)
    assert _opcode_cols([["Alpha", "Beta"], ["Gamma", "Delta"]]) is None


def _specs():
    base = [T("Base Specification", "2.4"), ("h", 1, "5 Admin Command Set"),
            ("table", "Figure 1: Opcodes for Admin Commands", ["Function", "", "Opcode", "Command", "Reference"],
             [["0001 00b", "", "10h", "3 Foo", "5.1"], ["1100 00b", "", "C0h to FFh", "Vendor specific", ""]], None),
            ("h", 2, "5.1 Foo command"), ("p", "The Foo command does a thing."),
            ("table", "Figure 2: Foo – Command Dword 10 and Command Dword 11", ["", "Bits", "", "Description"],
             [["63:00", "", "", "Starting Address (SADDR): a 64-bit address."]], None),
            ("table", "Figure 3: Foo – Command Dword 12", ["Bits", "", "Description"],
             [["08", "", "Flag (FLG): a one-bit flag. Refer to Figure 6 for unrelated codes."], ["07:00", "", "Mode (MODE): selects a mode. Refer to Figure 4."]], None),
            ("table", "Figure 6: Foo – Unrelated Codes", ["Value", "", "O/M", "Description"],
             [["10h", "", "M", "Gamma code."], ["20h", "", "O", "Delta code."], ["30h", "", "O", "Epsilon code."]], None),
            ("table", "Figure 4: Foo – Modes", ["Value", "", "O/M", "Description"],
             [["00h", "", "M", "Alpha selects the first behaviour."], ["01h", "", "O", "Beta selects the second behaviour."]], None),
            ("table", "Figure 4: Foo – Other Codes", ["Value", "", "O/M", "Description"],
             [["10h", "", "M", "Gamma code."], ["20h", "", "O", "Delta code."]], None),          # a different table that reuses the label
            ("table", "Figure 5: Foo – Completion Queue Entry Dword 0", ["Bits", "Description"], [["00", "Done (DN): the done flag."]], None)]
    mi = [("title", "NVM Express® Management Interface Specification", "Revision 2.0"), ("h", 1, "5 Commands"),
          ("table", "Figure 6: Opcodes for Management Interface Command Set", ["Opcode", "Command"], [["00h", "Bar"]], None),
          ("h", 2, "5.1 Bar"), ("p", "The Bar command does another thing."),
          ("table", "Figure 7: Bar – NVMe Management Dword 0", ["Bits", "Description"], [["07:00", "Selector (SEL): selects something."]], None)]
    return [("base-2.4.pdf", base), ("mi-2.0.pdf", mi)]


def test_catalog_reads_real_shaped_tables(conn, tmp_path):
    build(conn, tmp_path, _specs())
    res = catalog.build_catalog(conn, 1)
    names = {r["key"]: r["label"] for r in rows(conn, "SELECT key,label FROM catalog_item WHERE kind='command'")}
    assert names == {"cmd:admin:10": "Foo", "cmd:mi:00": "Bar"}                      # footnote digit stripped, vendor row skipped
    keys = {r["key"] for r in rows(conn, "SELECT key FROM catalog_item WHERE kind='field'")}
    assert {"cmd:admin:10/dw10[31:00]", "cmd:admin:10/dw11[31:00]"} <= keys         # the 63:00 address is addressed per dword
    assert not any("[63" in k for k in keys)
    assert not any(jloads(r["attrs_json"], {}).get("abbr") == "DN" for r in rows(conn, "SELECT attrs_json FROM catalog_item WHERE kind='field'"))    # CQE layout
    assert "cmd:mi:00/nmd0[07:00]" in keys                                          # management dwords use the decoder's names
    desc = {jloads(r["attrs_json"], {})["vlo"]: jloads(r["attrs_json"], {})["desc"] for r in rows(conn, "SELECT attrs_json FROM catalog_item WHERE kind='value'")
            if "MODE" in r["attrs_json"] or True}
    assert desc.get(0, "").startswith("Alpha") and desc.get(1, "").startswith("Beta")      # description column chosen by content
    flag = {r["key"]: jloads(r["attrs_json"], {}).get("desc") for r in rows(conn, "SELECT key,attrs_json FROM catalog_item WHERE kind='value' AND key LIKE 'cmd:admin:10/dw12[08:08]=%'")}
    assert set(flag.values()) == {"cleared", "set"}          # a cited table whose values cannot fit one bit is not the flag's value list
    mode = {jloads(r["attrs_json"], {})["vlo"] for r in rows(conn, "SELECT attrs_json FROM catalog_item WHERE kind='value' AND key LIKE 'cmd:admin:10/dw12[07:00]=%'")}
    assert mode == {0, 1}                                   # the other table that reuses 'Figure 4' is not folded into the enumeration
    assert res["version"]


def test_doubled_hyperlink_text_is_collapsed_so_references_resolve():
    from specforge.ingest.normalize import clean, extract_references

    assert clean("is shown in Figure 336Figure 336. Refer to section 5.2.14section 5.2.14 and Figure 12A Figure 12A.") == \
        "is shown in Figure 336. Refer to section 5.2.14 and Figure 12A."
    assert clean("Figure 3 and Figure 33 are listed") == "Figure 3 and Figure 33 are listed"          # distinct references stay distinct
    assert [r.label for r in extract_references(clean("defined in Figure 296Figure 296."), "nvme-base")] == ["296"]


def test_compact_table_keeps_the_header_over_its_own_column():
    assert compact_table(["Opcode", "O/M", "Command"], [["00h", "", "Foo"]]) == (["Opcode", "Command"], [["00h", "Foo"]])


def test_opcode_spellings_the_parser_accepts_are_accepted_here_too():
    assert _opcode_cols([["0x02", "Read"], ["04h\u20130Fh", "Reserved block"]]) == (0, 1)
    assert _opcode_cols([["3", "Read"], ["10", "Write"]]) is None                 # bare numbers are footnotes / references, not opcodes


def test_command_column_beats_reference_column_on_equal_scores():
    rows_ = [["00h", "Section 3.2", "Read"], ["01h", "Section 3.3", "Write"]]
    assert _opcode_cols(rows_) == (0, 2)                                            # a 'Section x' cell is a reference, never a name
    both = [["00h", "Basic thing", "Read"], ["01h", "Other thing", "Write"]]
    assert _opcode_cols(both, ["Opcode", "Description", "Command"]) == (0, 2)      # equal scores: the 'Command' header decides
    assert _opcode_cols(both) == (0, 1)                                              # no header: leftmost, not rightmost


def test_commands_that_merely_start_like_a_reference_word_are_still_names():
    assert _opcode_cols([["81h", "Security Send"], ["82h", "Security Receive"]]) == (0, 1)       # 'Sec' inside 'Security' is not a reference
    assert _opcode_cols([["81h", "Sec. 3.2", "Security Send"], ["82h", "Sec. 3.3", "Security Receive"]]) == (0, 2)
