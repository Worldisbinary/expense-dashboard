from __future__ import annotations

import io

import pytest

from expense_dashboard.money import (
    AmountError,
    currency_symbol,
    format_compact,
    format_inr,
    group_indian,
    parse_amount,
    rupees_to_paise,
)


@pytest.mark.parametrize(
    ("raw", "paise"),
    [
        ("100", 10_000),
        ("1,234.50", 1_23_450),
        ("1,23,456.78", 1_23_45_678),  # Indian lakh grouping
        ("₹2,500", 2_50_000),
        ("₹ 99.9", 9_990),
        ("Rs. 450", 45_000),
        ("Rs 450", 45_000),
        ("INR 1,000.00", 1_00_000),
        ("-250", -25_000),
        ("−250.00", -25_000),  # unicode minus sign
        ("+75", 7_500),
        ("(500.00)", -50_000),  # accounting negative
        ("1,200.00 Dr", -1_20_000),
        ("1,200.00 CR", 1_20_000),
        ("1,200.00 Dr.", -1_20_000),
        ("-₹24,512.00", -24_51_200),
        (".5", 50),
        ("10.005", 1_001),  # rounded half-up to the paisa
        ("10.004", 1_000),
        ("0.1", 10),
        (" 1 234 ", 1_23_400),
        ("1 234", 1_23_400),  # non-breaking space thousands separator
    ],
)
def test_parse_amount(raw, paise):
    assert parse_amount(raw) == paise


@pytest.mark.parametrize("raw", [None, "", "   ", "-", "--", "NA", "nil"])
def test_parse_amount_blank_cells_are_none(raw):
    assert parse_amount(raw) is None


@pytest.mark.parametrize(
    "raw", ["abc", "12abc", "1.2.3", "₹", "Rs.", "$5", "1e5", "1.234,50", "-1.234,00"]
)
def test_parse_amount_rejects_garbage(raw):
    with pytest.raises(AmountError):
        parse_amount(raw)


def test_parse_amount_is_exact_decimal_arithmetic():
    # 0.1 + 0.2 != 0.3 in float, but paise integers add exactly
    assert parse_amount("0.1") + parse_amount("0.2") == parse_amount("0.3")


def test_rupees_to_paise():
    assert rupees_to_paise("249") == 24_900
    assert rupees_to_paise(12.5) == 1_250
    for bad in ("0", "-5", "", "x"):
        with pytest.raises(AmountError):
            rupees_to_paise(bad)


@pytest.mark.parametrize(
    ("n", "text"),
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1,000"),
        (99_999, "99,999"),
        (1_00_000, "1,00,000"),
        (12_34_567, "12,34,567"),
        (1_23_45_678, "1,23,45,678"),
        (-1_00_000, "-1,00,000"),
    ],
)
def test_group_indian(n, text):
    assert group_indian(n) == text


def test_format_inr():
    assert format_inr(1_23_45_678, symbol="₹") == "₹1,23,456.78"
    assert format_inr(-5, symbol="₹") == "-₹0.05"
    assert format_inr(1_23_45_650, symbol="₹", decimals=False) == "₹1,23,457"
    assert format_inr(100, symbol="Rs.") == "Rs.1.00"


@pytest.mark.parametrize(
    ("rupees", "text"),
    [
        (0, "₹0"),
        (950, "₹950"),
        (1_000, "₹1k"),
        (12_500, "₹12.5k"),
        (1_00_000, "₹1L"),
        (2_50_000, "₹2.5L"),
        (3_00_00_000, "₹3Cr"),
        (-20_000, "-₹20k"),
    ],
)
def test_format_compact(rupees, text):
    assert format_compact(rupees) == text


def test_currency_symbol_falls_back_when_stream_cannot_encode_rupee():
    assert currency_symbol(io.TextIOWrapper(io.BytesIO(), encoding="utf-8")) == "₹"
    assert currency_symbol(io.TextIOWrapper(io.BytesIO(), encoding="cp1252")) == "Rs."
    assert currency_symbol(object()) == "Rs."  # no encoding attribute -> assume ascii
