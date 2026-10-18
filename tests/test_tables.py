from __future__ import annotations

import pytest

from expense_dashboard.tables import render_table


def test_render_table_aligns_columns():
    out = render_table(
        ["Name", "Amount"], [["Tea", "₹20.00"], ["Groceries", "₹1,250.00"]], align="lr"
    )
    lines = out.splitlines()
    assert lines[0] == "Name          Amount"
    assert lines[1] == "---------  ---------"
    assert lines[2] == "Tea           ₹20.00"
    assert lines[3] == "Groceries  ₹1,250.00"


def test_render_table_footer_and_none_cells():
    out = render_table(["A", "B"], [["x", None]], footer=["total", 3], align="lr")
    lines = out.splitlines()
    assert lines[2] == "x"  # trailing whitespace stripped, None rendered empty
    assert lines[3] == "-----  -"
    assert lines[4] == "total  3"


def test_render_table_truncates_long_cells():
    out = render_table(["D"], [["x" * 100]], max_width=10)
    assert out.splitlines()[2] == "xxxxxxx..."


def test_render_table_rejects_ragged_rows():
    with pytest.raises(ValueError, match="expected 2"):
        render_table(["A", "B"], [["only one"]])
