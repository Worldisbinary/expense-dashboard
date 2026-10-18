"""Extract-Transform-Load for bank-statement CSV files.

Pipeline
--------
1. **Extract**  read the CSV (skipping bank preamble lines) into raw dict rows.
2. **Transform** parse dates and ₹ amounts, normalise descriptions, decide debit vs
   credit, compute a stable dedupe hash, and auto-categorise with the rules table.
   Bad rows are collected as :class:`RowError` with their line number instead of
   aborting the whole file (unless ``strict``).
3. **Load**  in ONE transaction: insert an ``import_batches`` audit row, bulk-insert
   with ``executemany`` + ``INSERT OR IGNORE`` (the unique index on ``dedupe_hash``
   silently drops rows that were already imported), then write the batch counters.
   Any exception rolls back everything, so a failed import leaves no partial data.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from expense_dashboard.categorize import load_rules, match
from expense_dashboard.db import transaction
from expense_dashboard.money import AmountError, parse_amount

# --------------------------------------------------------------------------------------
# Column mapping
# --------------------------------------------------------------------------------------

#: Header aliases used for auto-detection (compared case-insensitively, punctuation-free).
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "txn date", "transaction date", "value date", "posting date", "tran date"),
    "description": (
        "description",
        "narration",
        "particulars",
        "remarks",
        "details",
        "transaction details",
        "transaction remarks",
    ),
    "debit": ("debit", "withdrawal", "withdrawal amt", "withdrawal amount", "debit amount", "dr"),
    "credit": ("credit", "deposit", "deposit amt", "deposit amount", "credit amount", "cr"),
    "amount": ("amount", "transaction amount", "amount inr", "amt"),
    # only unambiguous names: a generic 'Type' column often holds UPI/NEFT/IMPS instead,
    # so that one must be named explicitly with --direction-col
    "direction": ("dr cr", "cr dr", "debit credit", "dr cr indicator"),
}

_DEBIT_WORDS = {"dr", "d", "debit", "withdrawal", "db"}
_CREDIT_WORDS = {"cr", "c", "credit", "deposit"}


class MappingError(ValueError):
    """Raised when a CSV's columns cannot be mapped to date/description/amount."""


@dataclass(frozen=True)
class ColumnMapping:
    """How to read one bank's CSV layout.

    Either ``amount`` (one signed column) or ``debit``/``credit`` (split columns)
    must be given. ``amount_sign`` says which sign a debit has in a signed column:
    savings-account exports are usually ``debit_negative``; many credit-card exports
    list purchases as positive numbers (``debit_positive``). If the file has an
    unsigned amount plus a separate ``Dr``/``Cr`` column, name it as ``direction``.
    """

    date: str
    description: str
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None
    direction: str | None = None  # 'Dr'/'Cr' (or Debit/Credit) column next to ``amount``
    date_format: str | None = None  # strptime format; None = auto-detect (day-first)
    amount_sign: str = "debit_negative"
    delimiter: str = ","
    skip_rows: int = 0  # preamble lines before the header row
    encoding: str = "utf-8-sig"  # utf-8-sig transparently drops an Excel BOM

    def __post_init__(self) -> None:
        if self.amount is None and self.debit is None and self.credit is None:
            raise MappingError("mapping needs an 'amount' column or 'debit'/'credit' columns")
        if self.amount is not None and (self.debit is not None or self.credit is not None):
            raise MappingError("use either 'amount' or 'debit'/'credit', not both")
        if self.direction is not None and self.amount is None:
            raise MappingError("a 'direction' column only makes sense with an 'amount' column")
        if self.amount_sign not in ("debit_negative", "debit_positive"):
            raise MappingError("amount_sign must be 'debit_negative' or 'debit_positive'")
        if self.skip_rows < 0:
            raise MappingError("skip_rows must be >= 0")
        if len(self.delimiter) != 1:
            raise MappingError("delimiter must be a single character")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ColumnMapping:
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise MappingError(f"unknown mapping keys: {sorted(unknown)}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise MappingError(str(exc)) from exc

    @classmethod
    def from_json_file(cls, path: str | Path) -> ColumnMapping:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MappingError(f"{path}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise MappingError(f"{path}: mapping file must contain a JSON object")
        return cls.from_dict(data)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _norm_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def detect_mapping(headers: Iterable[str], **overrides: Any) -> ColumnMapping:
    """Guess a :class:`ColumnMapping` from header names; ``overrides`` win."""
    by_norm = {_norm_header(h): h for h in headers if h is not None}
    found: dict[str, str] = {}
    for role, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in by_norm:
                found[role] = by_norm[alias]
                break
    if "debit" in found or "credit" in found:
        found.pop("amount", None)  # split columns are more explicit than a generic 'amount'
        found.pop("direction", None)
    explicit_split = overrides.get("debit") or overrides.get("credit")
    if overrides.get("amount"):
        found.pop("debit", None)
        found.pop("credit", None)
    elif explicit_split:
        found.pop("amount", None)
        found.pop("direction", None)
    found.update({k: v for k, v in overrides.items() if v is not None})
    missing = [role for role in ("date", "description") if role not in found]
    if missing:
        raise MappingError(
            f"could not find column(s) for {missing} in headers {list(by_norm.values())}; "
            "pass --date-col/--desc-col or a --mapping file"
        )
    return ColumnMapping.from_dict(found)


# --------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------

_DAY_FIRST_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%b-%y",
    "%d %b %y",
    "%d %B %Y",
)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


class DateError(ValueError):
    """Raised when a date cell cannot be parsed."""


def parse_date(raw: str, fmt: str | None = None) -> date:
    """Parse a statement date.

    With an explicit ``fmt`` only that strptime format is accepted. Otherwise ISO
    ``YYYY-MM-DD`` (optionally followed by a time) is tried first, then Indian
    day-first layouts: ``DD/MM/YYYY``, ``DD-MM-YY``, ``05-Jan-2026``... US
    month-first dates are deliberately NOT guessed, because ``04/05/2026`` is
    ambiguous; pass ``--date-format %m/%d/%Y`` for such files.
    """
    text = (raw or "").strip()
    if not text:
        raise DateError("empty date")
    if fmt:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError as exc:
            raise DateError(f"date {raw!r} does not match format {fmt!r}") from exc
    if _ISO.match(text):
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise DateError(f"invalid ISO date {raw!r}") from exc
    for candidate in _DAY_FIRST_FORMATS:
        try:
            return datetime.strptime(text, candidate).date()
        except ValueError:
            continue
    raise DateError(f"unrecognised date {raw!r} (expected DD/MM/YYYY or YYYY-MM-DD)")


_WS = re.compile(r"\s+")


def normalize_description(raw: str) -> str:
    """Collapse whitespace and strip; keeps the original casing for display."""
    return _WS.sub(" ", (raw or "").strip())


def dedupe_key(txn_date: date, signed_paise: int, description: str, occurrence: int = 0) -> str:
    """Stable SHA-256 identity of a transaction.

    The key is built from the parts that do not change between two exports of the
    same statement: ISO date, signed amount in paise and the case-folded,
    whitespace-collapsed description. ``occurrence`` disambiguates genuinely
    identical rows inside one file (two ₹20 chai payments to the same stall on the
    same day): the first gets 0, the second 1, and so on. Re-importing the same
    file, or an overlapping statement, reproduces the same keys, which is what
    makes imports idempotent.
    """
    canonical = "|".join(
        (
            txn_date.isoformat(),
            str(signed_paise),
            normalize_description(description).casefold(),
            str(occurrence),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Transform
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedRow:
    line_no: int
    txn_date: date
    description: str
    amount_paise: int  # always > 0
    direction: str  # 'debit' | 'credit'
    dedupe_hash: str = ""
    category_id: int | None = None
    rule_id: int | None = None

    @property
    def signed_paise(self) -> int:
        return -self.amount_paise if self.direction == "debit" else self.amount_paise


@dataclass(frozen=True)
class RowError:
    line_no: int
    message: str


def _cell(row: dict[str, str | None], column: str | None) -> str | None:
    if column is None:
        return None
    if column not in row:
        raise MappingError(f"column {column!r} not found in CSV header")
    return row[column]


def transform_row(row: dict[str, str | None], mapping: ColumnMapping, line_no: int) -> ParsedRow:
    """Turn one raw CSV row into a :class:`ParsedRow` (without hash/category yet)."""
    txn_date = parse_date(_cell(row, mapping.date) or "", mapping.date_format)
    description = normalize_description(_cell(row, mapping.description) or "")
    if not description:
        raise ValueError("empty description")

    if mapping.amount is not None:
        signed = parse_amount(_cell(row, mapping.amount))
        if signed is None:
            raise AmountError("empty amount")
        if mapping.direction is not None:
            marker = (_cell(row, mapping.direction) or "").strip().rstrip(".").lower()
            if marker in _DEBIT_WORDS:
                signed = -abs(signed)
            elif marker in _CREDIT_WORDS:
                signed = abs(signed)
            else:
                raise AmountError(f"unknown debit/credit marker {marker!r}")
        elif mapping.amount_sign == "debit_positive":
            signed = -signed
    else:
        debit = parse_amount(_cell(row, mapping.debit))
        credit = parse_amount(_cell(row, mapping.credit))
        debit = abs(debit) if debit else 0
        credit = abs(credit) if credit else 0
        if debit and credit:
            raise AmountError("row has both a debit and a credit amount")
        signed = credit - debit

    if signed == 0:
        raise AmountError("amount is zero")
    direction = "debit" if signed < 0 else "credit"
    return ParsedRow(line_no, txn_date, description, abs(signed), direction)


@dataclass
class ImportResult:
    source_name: str
    rows_read: int = 0
    rows_inserted: int = 0
    rows_duplicate: int = 0
    rows_rejected: int = 0
    batch_id: int | None = None
    categorized: int = 0
    errors: list[RowError] = field(default_factory=list)
    dry_run: bool = False


class ImportAbortedError(ValueError):
    """Raised in strict mode when any row fails validation; nothing is written."""

    def __init__(self, errors: list[RowError]):
        self.errors = errors
        preview = "; ".join(f"line {e.line_no}: {e.message}" for e in errors[:5])
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        super().__init__(f"{len(errors)} invalid row(s): {preview}{more}")


def extract(
    text: str,
    mapping: ColumnMapping | None,
    overrides: dict[str, Any] | None = None,
) -> tuple[ColumnMapping, list[tuple[int, dict]]]:
    """Read CSV text into ``(line_no, row_dict)`` pairs.

    With ``mapping=None`` the columns are auto-detected from the header, and any
    ``overrides`` (e.g. from CLI flags such as ``--date-col``) take precedence.
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    lines = text.splitlines(keepends=True)
    skip = mapping.skip_rows if mapping else int(overrides.get("skip_rows", 0))
    delimiter = mapping.delimiter if mapping else overrides.get("delimiter", ",")
    body = "".join(lines[skip:])
    reader = csv.DictReader(io.StringIO(body), delimiter=delimiter)
    if reader.fieldnames is None:
        raise MappingError("CSV file is empty")
    reader.fieldnames = [(h or "").strip() for h in reader.fieldnames]
    if mapping is None:
        mapping = detect_mapping(reader.fieldnames, **overrides)
    rows: list[tuple[int, dict]] = []
    for record in reader:
        # skip blank lines and footer lines like ",,,," that banks append
        if not any((v or "").strip() for v in record.values() if isinstance(v, str)):
            continue
        # header is line skip+1, so the first data row is line skip+2 (1-based)
        rows.append((skip + reader.line_num, record))
    return mapping, rows


def transform(
    raw_rows: list[tuple[int, dict]], mapping: ColumnMapping, rules: list | None = None
) -> tuple[list[ParsedRow], list[RowError]]:
    """Parse, hash and categorise rows; return ``(good_rows, errors)``."""
    parsed: list[ParsedRow] = []
    errors: list[RowError] = []
    for line_no, record in raw_rows:
        try:
            parsed.append(transform_row(record, mapping, line_no))
        except MappingError:
            raise
        except ValueError as exc:  # DateError and AmountError are ValueErrors
            errors.append(RowError(line_no, str(exc)))

    seen: Counter[tuple[str, int, str]] = Counter()
    finished: list[ParsedRow] = []
    for row in parsed:
        base = (row.txn_date.isoformat(), row.signed_paise, row.description.casefold())
        occurrence = seen[base]
        seen[base] += 1
        rule = match(rules, row.description) if rules else None
        finished.append(
            ParsedRow(
                line_no=row.line_no,
                txn_date=row.txn_date,
                description=row.description,
                amount_paise=row.amount_paise,
                direction=row.direction,
                dedupe_hash=dedupe_key(row.txn_date, row.signed_paise, row.description, occurrence),
                category_id=rule.category_id if rule else None,
                rule_id=rule.id if rule else None,
            )
        )
    return finished, errors


# --------------------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------------------

_INSERT_SQL = """
INSERT OR IGNORE INTO transactions
    (txn_date, description, amount_paise, direction, category_id, rule_id,
     categorized_by, source, batch_id, dedupe_hash)
VALUES (?, ?, ?, ?, ?, ?, ?, 'import', ?, ?)
"""


def load(
    conn: sqlite3.Connection,
    rows: list[ParsedRow],
    *,
    source_name: str,
    file_sha256: str,
    mapping: ColumnMapping,
    rows_read: int,
    rows_rejected: int,
) -> tuple[int, int]:
    """Insert rows and the batch audit row atomically; return ``(batch_id, inserted)``."""
    with transaction(conn):
        batch_id = int(
            conn.execute(
                "INSERT INTO import_batches (source_name, file_sha256, mapping_json) "
                "VALUES (?, ?, ?)",
                (source_name, file_sha256, mapping.to_json()),
            ).lastrowid
        )
        params = [
            (
                r.txn_date.isoformat(),
                r.description,
                r.amount_paise,
                r.direction,
                r.category_id,
                r.rule_id,
                "rule" if r.category_id is not None else None,
                batch_id,
                r.dedupe_hash,
            )
            for r in rows
        ]
        cur = conn.executemany(_INSERT_SQL, params)
        # For executemany, sqlite3 sums the per-statement change counts; ignored
        # (duplicate) rows contribute 0, so this is exactly the number inserted.
        inserted = cur.rowcount if params else 0
        conn.execute(
            "UPDATE import_batches SET rows_read = ?, rows_inserted = ?, rows_duplicate = ?, "
            "rows_rejected = ? WHERE id = ?",
            (rows_read, inserted, len(rows) - inserted, rows_rejected, batch_id),
        )
    return batch_id, inserted


def import_csv(
    conn: sqlite3.Connection,
    path: str | Path,
    mapping: ColumnMapping | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    strict: bool = False,
    dry_run: bool = False,
) -> ImportResult:
    """Run the full extract-transform-load pipeline for one CSV file.

    Pass a complete ``mapping`` (e.g. from a JSON file), or leave it ``None`` to
    auto-detect columns from the header with optional ``overrides``.
    """
    path = Path(path)
    raw_bytes = path.read_bytes()
    if mapping is not None:
        encoding = mapping.encoding
    else:
        encoding = (overrides or {}).get("encoding") or "utf-8-sig"
    try:
        text = raw_bytes.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise MappingError(f"{path.name}: cannot decode as {encoding}; set 'encoding'") from exc

    mapping, raw_rows = extract(text, mapping, overrides)
    rules = load_rules(conn)
    rows, errors = transform(raw_rows, mapping, rules)
    result = ImportResult(
        source_name=path.name,
        rows_read=len(raw_rows),
        rows_rejected=len(errors),
        errors=errors,
        dry_run=dry_run,
        categorized=sum(1 for r in rows if r.category_id is not None),
    )
    if errors and strict:
        raise ImportAbortedError(errors)
    if dry_run:
        existing = _count_existing(conn, [r.dedupe_hash for r in rows])
        result.rows_inserted = len(rows) - existing
        result.rows_duplicate = existing
        return result

    batch_id, inserted = load(
        conn,
        rows,
        source_name=path.name,
        file_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        mapping=mapping,
        rows_read=len(raw_rows),
        rows_rejected=len(errors),
    )
    result.batch_id = batch_id
    result.rows_inserted = inserted
    result.rows_duplicate = len(rows) - inserted
    return result


def _count_existing(conn: sqlite3.Connection, hashes: list[str]) -> int:
    count = 0
    for h in hashes:  # each probe is an O(log n) seek on ux_transactions_dedupe_hash
        if conn.execute("SELECT 1 FROM transactions WHERE dedupe_hash = ?", (h,)).fetchone():
            count += 1
    return count


def undo_batch(conn: sqlite3.Connection, batch_id: int) -> int:
    """Delete an import batch and (via ON DELETE CASCADE) every row it inserted."""
    with transaction(conn):
        removed = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        cur = conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
        if cur.rowcount != 1:
            raise ValueError(f"no import batch with id {batch_id}")
    return int(removed)
