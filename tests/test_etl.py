from __future__ import annotations

import json
from datetime import date

import pytest

from expense_dashboard import etl

BANK_CSV = """\
Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance
01/03/2026,NEFT/ACME LTD/SALARY MAR,,"95,000.00","1,45,000.00"
02/03/2026,UPI/SWIGGY/123456789012,345.00,,"1,44,655.00"
02/03/2026,UPI/OLA CABS/123456789013,"1,210.50",,"1,43,444.50"
05/03/2026,NACH/ZERODHA BROKING/SIP,"5,000.00",,"1,38,444.50"
"""


def _rows(conn):
    return conn.execute(
        "SELECT t.txn_date, t.description, t.amount_paise, t.direction, c.name AS category "
        "FROM transactions t LEFT JOIN categories c ON c.id = t.category_id ORDER BY t.id"
    ).fetchall()


# ---------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("05/03/2026", date(2026, 3, 5)),  # DD/MM/YYYY, not US MM/DD
        ("31/12/2025", date(2025, 12, 31)),
        ("05-03-2026", date(2026, 3, 5)),
        ("05.03.2026", date(2026, 3, 5)),
        ("05/03/26", date(2026, 3, 5)),
        ("2026-03-05", date(2026, 3, 5)),
        ("2026-03-05 14:22:01", date(2026, 3, 5)),
        ("2026-03-05T14:22:01", date(2026, 3, 5)),
        ("05-Mar-2026", date(2026, 3, 5)),
        ("05 Mar 2026", date(2026, 3, 5)),
        ("05-mar-26", date(2026, 3, 5)),
        ("5 March 2026", date(2026, 3, 5)),
        (" 29/02/2024 ", date(2024, 2, 29)),  # leap day
    ],
)
def test_parse_date_formats(raw, expected):
    assert etl.parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "  ", "31/02/2026", "2026-02-30", "13/13/2026", "yesterday", "2026/03/05"]
)
def test_parse_date_rejects_invalid(raw):
    with pytest.raises(etl.DateError):
        etl.parse_date(raw)


def test_parse_date_explicit_format_for_us_files():
    assert etl.parse_date("03/05/2026", "%m/%d/%Y") == date(2026, 3, 5)
    with pytest.raises(etl.DateError, match="does not match"):
        etl.parse_date("2026-03-05", "%m/%d/%Y")


# ---------------------------------------------------------------- mapping


def test_detect_mapping_split_columns():
    m = etl.detect_mapping(
        ["Date", "Narration", "Chq./Ref.No.", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]
    )
    assert (m.date, m.description, m.debit, m.credit, m.amount) == (
        "Date",
        "Narration",
        "Withdrawal Amt.",
        "Deposit Amt.",
        None,
    )


def test_detect_mapping_signed_amount_and_case_insensitive():
    m = etl.detect_mapping(["TRANSACTION DATE", "particulars", "Amount (INR)"])
    assert (m.date, m.description, m.amount) == ("TRANSACTION DATE", "particulars", "Amount (INR)")


def test_detect_mapping_overrides_win():
    m = etl.detect_mapping(
        ["When", "What", "Debit", "Credit", "Net"], date="When", description="What", amount="Net"
    )
    assert (m.date, m.amount, m.debit, m.credit) == ("When", "Net", None, None)
    m = etl.detect_mapping(["Date", "Details", "Amount", "Out"], debit="Out")
    assert (m.amount, m.debit) == (None, "Out")


def test_detect_mapping_missing_columns():
    with pytest.raises(etl.MappingError, match="date"):
        etl.detect_mapping(["Narration", "Amount"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # no amount columns at all
        {"amount": "A", "debit": "D"},
        {"amount": "A", "amount_sign": "weird"},
        {"amount": "A", "skip_rows": -1},
        {"amount": "A", "delimiter": ";;"},
    ],
)
def test_column_mapping_validation(kwargs):
    with pytest.raises(etl.MappingError):
        etl.ColumnMapping(date="D", description="N", **kwargs)


def test_mapping_json_round_trip(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "date": "Txn Date",
                "description": "Remarks",
                "amount": "Amt",
                "amount_sign": "debit_positive",
            }
        )
    )
    m = etl.ColumnMapping.from_json_file(path)
    assert m.amount_sign == "debit_positive"
    assert json.loads(m.to_json())["description"] == "Remarks"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "invalid JSON"),
        ("[1, 2]", "JSON object"),
        ('{"date": "D", "description": "N", "amount": "A", "colour": "red"}', "unknown"),
    ],
)
def test_mapping_json_errors(tmp_path, content, message):
    path = tmp_path / "m.json"
    path.write_text(content)
    with pytest.raises(etl.MappingError, match=message):
        etl.ColumnMapping.from_json_file(path)


def test_mapping_from_dict_missing_required_key():
    with pytest.raises(etl.MappingError):
        etl.ColumnMapping.from_dict({"date": "D", "amount": "A"})


# ---------------------------------------------------------------- transform


def _transform(text, mapping=None, **overrides):
    mapping, raw = etl.extract(text, mapping, overrides)
    return etl.transform(raw, mapping)


def test_transform_split_debit_credit():
    rows, errors = _transform(BANK_CSV)
    assert errors == []
    assert [(r.direction, r.amount_paise) for r in rows] == [
        ("credit", 95_000_00),
        ("debit", 345_00),
        ("debit", 1_210_50),
        ("debit", 5_000_00),
    ]
    assert rows[0].txn_date == date(2026, 3, 1)


def test_transform_signed_amount_conventions():
    text = "Date,Description,Amount\n2026-03-01,COFFEE,-120.00\n2026-03-02,REFUND,50\n"
    rows, _ = _transform(text)
    assert [(r.direction, r.amount_paise) for r in rows] == [("debit", 12000), ("credit", 5000)]
    # credit-card style: purchases are positive
    rows, _ = _transform(text, amount_sign="debit_positive")
    assert [r.direction for r in rows] == ["credit", "debit"]


def test_transform_collects_bad_rows_with_line_numbers():
    text = (
        "Date,Narration,Debit,Credit\n"
        "01/03/2026,GOOD ROW,100,\n"
        "31/02/2026,BAD DATE,100,\n"
        "02/03/2026,BAD AMOUNT,12abc,\n"
        "02/03/2026,   ,100,\n"
        "02/03/2026,BOTH SIDES,100,100\n"
        "02/03/2026,ZERO,0.00,0.00\n"
        "02/03/2026,NO AMOUNT,,\n"
    )
    rows, errors = _transform(text)
    assert [r.description for r in rows] == ["GOOD ROW"]
    assert [e.line_no for e in errors] == [3, 4, 5, 6, 7, 8]
    messages = " | ".join(e.message for e in errors)
    for fragment in (
        "31/02/2026",
        "12abc",
        "empty description",
        "both a debit and a credit",
        "zero",
    ):
        assert fragment in messages


def test_transform_skips_blank_and_footer_lines_and_handles_preamble_bom_quotes():
    text = (
        "﻿XYZ Bank statement\n"
        "Account 1234\n"
        "Date;Narration;Amount\n"
        '01/03/2026;"AMAZON, PAY INDIA";-1.234,00\n'
        ";;\n"
        "\n"
        "02/03/2026;  multiple   spaces  ;-10\n"
    )
    # European-style decimal comma is NOT supported: it is reported, not misread
    rows, errors = _transform(text, skip_rows=2, delimiter=";")
    assert [r.description for r in rows] == ["multiple spaces"]
    assert len(errors) == 1 and errors[0].line_no == 4


def test_transform_line_numbers_account_for_preamble():
    text = "preamble\nDate,Narration,Amount\n01/03/2026,OK,-1\n01/03/2026,BAD,-x\n"
    _, errors = _transform(text, skip_rows=1)
    assert errors[0].line_no == 4


def test_transform_unknown_column_is_a_mapping_error():
    mapping = etl.ColumnMapping(date="Date", description="Nope", amount="Amount")
    with pytest.raises(etl.MappingError, match="Nope"):
        _transform("Date,Narration,Amount\n01/03/2026,X,-1\n", mapping)


def test_extract_empty_file():
    with pytest.raises(etl.MappingError, match="empty"):
        etl.extract("", None)


# ---------------------------------------------------------------- hashing


def test_dedupe_key_is_stable_and_normalised():
    a = etl.dedupe_key(date(2026, 3, 2), -34500, "UPI/SWIGGY/123")
    b = etl.dedupe_key(date(2026, 3, 2), -34500, "  upi/swiggy/123 ")
    assert a == b and len(a) == 64
    assert a != etl.dedupe_key(date(2026, 3, 2), 34500, "UPI/SWIGGY/123")  # sign matters
    assert a != etl.dedupe_key(date(2026, 3, 3), -34500, "UPI/SWIGGY/123")
    assert a != etl.dedupe_key(date(2026, 3, 2), -34500, "UPI/SWIGGY/123", occurrence=1)


def test_identical_rows_in_one_file_get_distinct_hashes():
    text = "Date,Narration,Amount\n" + "02/03/2026,TEA STALL,-20\n" * 3
    rows, _ = _transform(text)
    assert len({r.dedupe_hash for r in rows}) == 3


# ---------------------------------------------------------------- load / import


def test_import_inserts_categorises_and_logs_batch(conn, write_csv):
    path = write_csv("bank.csv", BANK_CSV)
    result = etl.import_csv(conn, path)
    assert (
        result.rows_read,
        result.rows_inserted,
        result.rows_duplicate,
        result.rows_rejected,
    ) == (4, 4, 0, 0)
    assert [r["category"] for r in _rows(conn)] == [
        "Income",
        "Food Delivery",
        "Transport",
        "Investments",
    ]
    batch = conn.execute("SELECT * FROM import_batches").fetchone()
    assert batch["rows_inserted"] == 4 and batch["source_name"] == "bank.csv"
    assert json.loads(batch["mapping_json"])["debit"] == "Withdrawal Amt."
    provenance = conn.execute("SELECT DISTINCT categorized_by, source FROM transactions").fetchall()
    assert [tuple(r) for r in provenance] == [("rule", "import")]


def test_reimport_is_idempotent(conn, write_csv):
    path = write_csv("bank.csv", BANK_CSV)
    etl.import_csv(conn, path)
    before = _rows(conn)
    again = etl.import_csv(conn, path)
    assert (again.rows_inserted, again.rows_duplicate) == (0, 4)
    assert _rows(conn) == before
    batches = conn.execute("SELECT rows_inserted, rows_duplicate FROM import_batches").fetchall()
    assert [tuple(b) for b in batches] == [(4, 0), (0, 4)]  # the no-op import is still audited


def test_overlapping_statement_only_adds_new_rows(conn, write_csv):
    etl.import_csv(conn, write_csv("feb_mar.csv", BANK_CSV))
    overlap = (
        "Date,Narration,Withdrawal Amt.,Deposit Amt.\n"
        # same rows, different formatting and casing: still duplicates
        "05/03/2026,nach/zerodha broking/sip,5000,\n"
        "02/03/2026,UPI/OLA CABS/123456789013,1210.50,\n"
        "06/03/2026,UPI/ZOMATO/999,220.00,\n"
    )
    result = etl.import_csv(conn, write_csv("mar.csv", overlap))
    assert (result.rows_inserted, result.rows_duplicate) == (1, 2)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 5


def test_identical_rows_survive_reimport_without_duplication(conn, write_csv):
    text = "Date,Narration,Amount\n" + "02/03/2026,TEA STALL,-20\n" * 2
    path = write_csv("tea.csv", text)
    assert etl.import_csv(conn, path).rows_inserted == 2
    assert etl.import_csv(conn, path).rows_inserted == 0


def test_strict_mode_aborts_without_writing(conn, write_csv):
    path = write_csv("bad.csv", BANK_CSV + "99/99/2026,BROKEN,1,\n")
    with pytest.raises(etl.ImportAbortedError, match="line 6"):
        etl.import_csv(conn, path, strict=True)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0


def test_import_aborted_error_message_truncates():
    err = etl.ImportAbortedError([etl.RowError(i, "bad") for i in range(8)])
    assert "(+3 more)" in str(err)


def test_lenient_mode_imports_good_rows_and_counts_rejects(conn, write_csv):
    path = write_csv("bad.csv", BANK_CSV + "99/99/2026,BROKEN,1,\n")
    result = etl.import_csv(conn, path)
    assert (result.rows_read, result.rows_inserted, result.rows_rejected) == (5, 4, 1)
    batch = conn.execute("SELECT rows_read, rows_rejected FROM import_batches").fetchone()
    assert tuple(batch) == (5, 1)


def test_dry_run_writes_nothing_but_predicts_counts(conn, write_csv):
    path = write_csv("bank.csv", BANK_CSV)
    dry = etl.import_csv(conn, path, dry_run=True)
    assert (dry.rows_inserted, dry.rows_duplicate, dry.batch_id) == (4, 0, None)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    etl.import_csv(conn, path)
    dry = etl.import_csv(conn, path, dry_run=True)
    assert (dry.rows_inserted, dry.rows_duplicate) == (0, 4)


def test_import_is_atomic_when_load_fails_midway(conn, write_csv, monkeypatch):
    """If anything fails after the batch row is written, nothing persists."""
    path = write_csv("bank.csv", BANK_CSV)
    real_execute = conn.execute

    class Wrapped:
        """Proxy the connection; blow up on the final UPDATE of the batch counters."""

        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE import_batches"):
                raise RuntimeError("disk on fire")
            return real_execute(sql, *args)

    with pytest.raises(RuntimeError, match="disk on fire"):
        etl.import_csv(Wrapped(), path)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    assert not conn.in_transaction


def test_import_with_explicit_mapping_and_encoding(conn, write_csv):
    text = "Txn Date|Remarks|Amt\n2026-03-07|ZEPTO MARKETPLACE|₹1,299.00\n"
    path = write_csv("card.csv", text, encoding="utf-16")
    mapping = etl.ColumnMapping(
        date="Txn Date",
        description="Remarks",
        amount="Amt",
        amount_sign="debit_positive",
        delimiter="|",
        encoding="utf-16",
    )
    result = etl.import_csv(conn, path, mapping)
    assert result.rows_inserted == 1
    assert tuple(_rows(conn)[0]) == (
        "2026-03-07",
        "ZEPTO MARKETPLACE",
        129900,
        "debit",
        "Groceries",
    )


def test_import_wrong_encoding_is_reported(conn, write_csv):
    path = write_csv("x.csv", "Date,Narration,Amount\n01/01/2026,CAFÉ,-1\n", encoding="utf-16")
    with pytest.raises(etl.MappingError, match="decode"):
        etl.import_csv(conn, path)
    with pytest.raises(etl.MappingError, match="decode"):
        etl.import_csv(conn, path, overrides={"encoding": "no-such-codec"})


def test_import_empty_file_logs_nothing_on_error(conn, write_csv):
    path = write_csv("empty.csv", "Date,Narration,Amount\n")
    result = etl.import_csv(conn, path)
    assert (result.rows_read, result.rows_inserted) == (0, 0)


def test_undo_batch_removes_only_that_batch(conn, write_csv):
    first = etl.import_csv(conn, write_csv("a.csv", BANK_CSV))
    second = etl.import_csv(
        conn, write_csv("b.csv", "Date,Narration,Amount\n09/03/2026,UPI/UBER/1,-99\n")
    )
    assert etl.undo_batch(conn, second.batch_id) == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4
    assert etl.undo_batch(conn, first.batch_id) == 4
    with pytest.raises(ValueError, match="no import batch"):
        etl.undo_batch(conn, first.batch_id)


# ---------------------------------------------------------------- Dr/Cr indicator column


def test_direction_column_sets_the_sign():
    text = (
        "Value Date,Transaction Remarks,Amount (INR),Dr/Cr\n"
        "05-Mar-2026,ZEPTO,1299.00,Dr\n"
        "06-Mar-2026,REFUND,-145.00,CR.\n"  # the marker wins over a stray sign
        "07-Mar-2026,ODD,10,X\n"
    )
    mapping, raw = etl.extract(text, None)
    assert mapping.direction == "Dr/Cr" and mapping.amount == "Amount (INR)"
    rows, errors = etl.transform(raw, mapping)
    assert [(r.direction, r.amount_paise) for r in rows] == [("debit", 129900), ("credit", 14500)]
    assert "unknown debit/credit marker 'x'" in errors[0].message


def test_generic_type_column_is_not_mistaken_for_direction():
    mapping = etl.detect_mapping(["Date", "Narration", "Type", "Amount"])
    assert mapping.direction is None


def test_direction_requires_amount_column():
    with pytest.raises(etl.MappingError, match="direction"):
        etl.ColumnMapping(date="D", description="N", debit="X", direction="T")
    m = etl.detect_mapping(["Date", "Narration", "Debit", "Credit", "Dr/Cr"])
    assert m.direction is None  # split columns already carry the sign


# ---------------------------------------------------------------- shipped examples


def test_shipped_examples_import_cleanly(conn):
    from pathlib import Path

    examples = Path(__file__).resolve().parents[1] / "examples"
    results = [
        etl.import_csv(conn, examples / "sample_statement.csv"),
        etl.import_csv(conn, examples / "drcr_export.csv"),
        etl.import_csv(
            conn,
            examples / "custom_export.csv",
            etl.ColumnMapping.from_json_file(examples / "custom_mapping.json"),
        ),
    ]
    assert [(r.rows_read, r.rows_inserted, r.rows_rejected) for r in results] == [
        (16, 16, 0),
        (4, 4, 0),
        (3, 3, 0),
    ]
    payment = conn.execute(
        "SELECT t.txn_date, t.direction, c.name FROM transactions t "
        "JOIN categories c ON c.id = t.category_id WHERE t.description LIKE 'PAYMENT RECEIVED%'"
    ).fetchone()
    assert tuple(payment) == ("2026-03-18", "credit", "Credit Card Payment")
