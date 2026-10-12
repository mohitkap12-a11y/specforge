from specforge.ingest.normalize import (
    classify_table, detect_identity, extract_references, normative_info, parse_bit_range, parse_byte_range,
    parse_caption, parse_hex_value, parse_value_range, split_sentences,
)


def test_bit_ranges():
    assert parse_bit_range("31:16") == (31, 16)
    assert parse_bit_range("07") == (7, 7)
    assert parse_bit_range("15–08") == (15, 8)       # en dash
    assert parse_bit_range("Bits 7:0") == (7, 0)
    assert parse_bit_range("hello") is None
    assert parse_byte_range("03:00") == (3, 0)
    assert parse_byte_range("1023") == (1023, 1023)


def test_values():
    assert parse_hex_value("01h") == 1
    assert parse_hex_value("0x1F") == 31
    assert parse_hex_value("011b") == 3
    assert parse_value_range("04h to 0Fh") == (4, 15)
    assert parse_value_range("100b to 111b") == (4, 7)
    assert parse_value_range("80h-FFh") == (128, 255)
    assert parse_value_range("Reserved") is None


def test_caption():
    assert parse_caption("Figure 312: Identify – Command Dword 10") == ("figure", "312", "Identify – Command Dword 10", False)
    assert parse_caption("Figure 312: Identify – Command Dword 10 (continued)")[3] is True
    assert parse_caption("Table 5. Something")[:2] == ("table", "5")
    assert parse_caption("Figures are nice") is None


def test_sentences_and_normative():
    s = split_sentences("The controller shall abort the command, e.g. with status 5.1.13. The host may retry.")
    assert len(s) == 2 and s[0].endswith("5.1.13.")
    assert normative_info("The host shall not set it")[0] == "prohibited"
    assert normative_info("The host should set it")[0] == "recommended"
    assert normative_info("The host may set it")[0] == "optional"
    assert normative_info("This is informative") is None


def test_references_attach_nearest_spec():
    refs = extract_references("Refer to section 5.1.13 of the NVM Express Base Specification and Figure 313. See Figure 4.", "nvme-nvm-cs")
    got = {(r.kind, r.label, r.family) for r in refs}
    assert ("section", "5.1.13", "nvme-base") in got
    assert ("figure", "313", "nvme-base") in got
    assert ("figure", "4", None) in got
    # references to the own spec by name stay local
    r2 = extract_references("as described in section 3 of the NVM Express NVM Command Set Specification", "nvme-nvm-cs")
    assert r2[0].family is None


def test_table_classification():
    assert classify_table(["Bits", "Description"], "") == "cdw"
    assert classify_table(["Bytes", "O/M", "Description"], "") == "bytes"
    assert classify_table(["Opcode", "O/M", "Command"], "") == "opcode"
    assert classify_table(["CNS Value", "O/M", "Description"], "") == "values"
    assert classify_table(["Name", "Meaning"], "") == "other"


def test_identity_prefers_title_page():
    page1 = "NVM Express® Base Specification\nRevision 2.4\n"
    page2 = "\fSee also the NVM Command Set Specification"
    assert detect_identity(page1 + page2) == ("nvme-base", "2.4")
    assert detect_identity("NVM Express® Management Interface Specification\nRevision 2.0") == ("nvme-mi", "2.0")
    assert detect_identity("", "NVM-Express-NVM-Command-Set-Specification-Revision-1.3-Ratified.pdf") == ("nvme-nvm-cs", "1.3")
