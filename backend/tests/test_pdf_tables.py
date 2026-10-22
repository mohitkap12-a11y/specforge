"""Table headers wrapped over several lines, and tables continued on the next page under a repeated caption."""
from specforge.ingest.ir import Table
from specforge.ingest.pdf import _merge_continuation, _split_header

HEAD = ["Feature Identifier", "", "Current Setting", "", "Scope"]
WRAP = [["", "", "Persists", "", ""], ["", "", "Across Power", "", ""], ["", "", "Cycle and Reset", "", ""]]


def test_a_heading_wrapped_over_lines_is_joined_into_the_header():
    header, body = _split_header([HEAD, *WRAP, ["04h", "No", "", "", "Controller"]])
    assert header == ["Feature Identifier", "", "Current Setting Persists Across Power Cycle and Reset", "", "Scope"]
    assert body == [["04h", "No", "", "", "Controller"]]


def test_a_row_without_a_key_below_a_header_is_only_wrapped_text_when_it_comes_first():
    rows = [["Bits", "Description"], ["31:11", "Reserved"], ["", "continued description of the row above"]]
    header, body = _split_header(rows)
    assert header == ["Bits", "Description"] and body == rows[1:]


def test_long_text_in_a_keyless_first_row_is_data_not_heading():
    rows = [["Bits", "Description"], ["", "x" * 80]]
    assert _split_header(rows) == (["Bits", "Description"], rows[1:])


def test_the_repeated_wrapped_heading_of_a_continuation_page_is_dropped():
    first_header, first_body = _split_header([HEAD, *WRAP, ["00h", "Reserved", "", "", ""]])
    t = Table(label="466", caption="Set Features", header=first_header, rows=first_body, page_from=1, page_to=1)
    _merge_continuation(t, [HEAD, *WRAP, ["04h", "No", "", "", "Controller"]], 2)
    assert [r[0] for r in t.rows] == ["00h", "04h"] and t.page_to == 2


def test_a_heading_is_limited_to_the_configured_number_of_lines():
    rows = [["Name", "Info"], *[["", f"line {i}"] for i in range(1, 7)]]
    header, body = _split_header(rows)
    assert header[1] == "Info line 1 line 2 line 3 line 4" and len(body) == 2


def test_a_row_fragment_after_the_repeated_heading_is_glued_to_the_previous_row():
    first_header, first_body = _split_header([HEAD, *WRAP, ["00h", "Reserved", "", "", "Contr"]])
    t = Table(label="466", caption="Set Features", header=first_header, rows=first_body, page_from=1, page_to=1)
    _merge_continuation(t, [HEAD, *WRAP, ["", "", "", "", "oller"], ["04h", "No", "", "", "Controller"]], 2)
    assert [r[0] for r in t.rows] == ["00h", "04h"] and t.rows[0][4] == "Contr oller"
