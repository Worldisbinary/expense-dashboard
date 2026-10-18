"""Parsing and formatting Indian-rupee amounts.

All amounts are handled as integer paise internally. Parsing goes through
:class:`decimal.Decimal`, never ``float``, so ``"0.1" + "0.2"`` really is 30 paise.
"""

from __future__ import annotations

import re
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

RUPEE = "₹"

# Currency markers that show up in bank exports: ₹1,234.00 / Rs. 1,234 / INR 1234
_CURRENCY_TOKENS = re.compile(r"(₹|\bINR\b|\bRs\b\.?)", re.IGNORECASE)
_DR_CR_SUFFIX = re.compile(r"\s*\b(DR|CR)\b\.?\s*$", re.IGNORECASE)
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
_EMPTY_MARKERS = {"", "-", "--", "—", "na", "n/a", "nil"}


class AmountError(ValueError):
    """Raised when a string cannot be read as a money amount."""


def parse_amount(raw: str | None) -> int | None:
    """Parse a statement amount into signed paise.

    Returns ``None`` for blank cells (common in split Debit/Credit columns).
    Understands Indian digit grouping (``1,23,456.78``), currency prefixes
    (``₹``, ``Rs.``, ``INR``), a unicode minus, accounting parentheses
    (``(500.00)`` means -500) and ``Dr``/``Cr`` suffixes (``Dr`` means negative).
    Values with more than two decimals are rounded half-up to the nearest paisa.
    """
    if raw is None:
        return None
    text = raw.strip()
    if text.lower() in _EMPTY_MARKERS:
        return None

    negative = False
    suffix = _DR_CR_SUFFIX.search(text)
    if suffix:
        negative = suffix.group(1).upper() == "DR"
        text = text[: suffix.start()]
    if text.startswith("(") and text.endswith(")"):
        negative = not negative
        text = text[1:-1]

    text = _CURRENCY_TOKENS.sub("", text)
    if "," in text and "." in text and text.rfind(",") > text.rfind("."):
        # '1.234,50' is a European decimal comma; stripping commas would silently
        # turn it into 1.23450 rupees, so refuse instead of guessing
        raise AmountError(f"decimal-comma amounts are not supported: {raw!r}")
    text = text.replace("−", "-").replace(",", "").replace(" ", "").replace(" ", "")
    if text.startswith("-"):
        negative = not negative
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]

    if not _NUMBER.match(text):
        raise AmountError(f"not a valid amount: {raw!r}")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards this
        raise AmountError(f"not a valid amount: {raw!r}") from exc

    paise = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -paise if negative else paise


def rupees_to_paise(value: str | int | float | Decimal) -> int:
    """Convert a user-entered rupee value (CLI argument) to paise; must be > 0."""
    paise = parse_amount(str(value))
    if paise is None or paise <= 0:
        raise AmountError(f"amount must be a positive number of rupees, got {value!r}")
    return paise


def group_indian(integer_part: int) -> str:
    """Group digits the Indian way: 12345678 -> '1,23,45,678'."""
    digits = str(abs(integer_part))
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        if head:
            pairs.insert(0, head)
        grouped = ",".join([*pairs, tail])
    return f"-{grouped}" if integer_part < 0 else grouped


def format_inr(paise: int, *, symbol: str | None = None, decimals: bool = True) -> str:
    """Format paise as rupees with Indian grouping, e.g. 12345678 -> '₹1,23,456.78'."""
    sym = currency_symbol() if symbol is None else symbol
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    if decimals:
        rupees, rem = divmod(paise, 100)
        return f"{sign}{sym}{group_indian(rupees)}.{rem:02d}"
    rupees = int((Decimal(paise) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{sign}{sym}{group_indian(rupees)}"


def format_compact(rupees: float, symbol: str = RUPEE) -> str:
    """Short axis labels in Indian units: 950 -> ₹950, 12500 -> ₹12.5k, 250000 -> ₹2.5L."""
    sign = "-" if rupees < 0 else ""
    value = abs(rupees)
    for divisor, unit in ((1_00_00_000, "Cr"), (1_00_000, "L"), (1_000, "k")):
        if value >= divisor:
            number = f"{value / divisor:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{symbol}{number}{unit}"
    return f"{sign}{symbol}{value:.0f}"


def currency_symbol(stream: object = None) -> str:
    """'₹' if the output stream can encode it, else 'Rs.' (e.g. a cp1252 Windows pipe)."""
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        RUPEE.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "Rs."
    return RUPEE
