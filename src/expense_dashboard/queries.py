"""SQL queries: inserts, filtered retrieval and aggregations.

Every aggregate is computed on demand from the ``transactions`` table (no
materialised summary tables), so ``summary`` and ``report`` always reflect the rows
inserted a moment ago. The heavy queries are kept as module-level SQL templates so
``expense explain`` can run ``EXPLAIN QUERY PLAN`` on exactly the SQL the app runs.

The ``{hint}`` placeholder sits right after ``FROM transactions AS t``. It is empty
for normal execution; ``explain --benchmark`` substitutes ``NOT INDEXED`` to force a
full table scan for comparison.
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from expense_dashboard.categorize import RuleError, category_id_by_name, load_rules, match
from expense_dashboard.db import transaction
from expense_dashboard.etl import dedupe_key, normalize_description

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

#: A debit counts as spending unless it is categorised as income or a transfer.
SPEND_FILTER = "t.direction = 'debit' AND (c.kind IS NULL OR c.kind = 'expense')"
#: A credit counts as income unless it is categorised as an expense (refund) or transfer.
INCOME_FILTER = "t.direction = 'credit' AND (c.kind IS NULL OR c.kind = 'income')"


# --------------------------------------------------------------------------------------
# Month helpers
# --------------------------------------------------------------------------------------


def parse_month(text: str) -> str:
    """Validate ``YYYY-MM`` and return it unchanged."""
    if not _MONTH.match(text or ""):
        raise ValueError(f"month must look like YYYY-MM, got {text!r}")
    return text


def month_bounds(month: str) -> tuple[str, str]:
    """Half-open ISO range for a month: ``('2026-02-01', '2026-03-01')``.

    Half-open ``>= start AND < next_start`` ranges avoid the classic
    ``BETWEEN '...-01' AND '...-31'`` bug for 28/30-day months.
    """
    year, mon = map(int, parse_month(month).split("-"))
    start = date(year, mon, 1)
    nxt = date(year + (mon == 12), mon % 12 + 1, 1)
    return start.isoformat(), nxt.isoformat()


def add_months(month: str, delta: int) -> str:
    year, mon = map(int, parse_month(month).split("-"))
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_range(first: str, last: str) -> list[str]:
    """Inclusive list of months from ``first`` to ``last``."""
    out, cur = [], parse_month(first)
    parse_month(last)
    while cur <= last:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def latest_month(conn: sqlite3.Connection) -> str | None:
    """Month of the most recent transaction (uses idx_transactions_date: MAX is one seek)."""
    row = conn.execute("SELECT MAX(txn_date) FROM transactions").fetchone()
    return row[0][:7] if row and row[0] else None


# --------------------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------------------


def list_categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.id, c.name, c.kind,
               (SELECT COUNT(*) FROM transactions t WHERE t.category_id = c.id) AS txn_count,
               (SELECT COUNT(*) FROM rules r WHERE r.category_id = c.id) AS rule_count
        FROM categories c
        ORDER BY c.kind, c.name
        """
    ).fetchall()


def add_category(conn: sqlite3.Connection, name: str, kind: str = "expense") -> int:
    try:
        cur = conn.execute(
            "INSERT INTO categories (name, kind) VALUES (?, ?)", (name.strip(), kind)
        )
    except sqlite3.IntegrityError as exc:
        raise RuleError(f"category {name!r} already exists or is invalid") from exc
    return int(cur.lastrowid)


def delete_category(conn: sqlite3.Connection, name: str) -> None:
    """Delete a category; its transactions become uncategorised, its rules and budgets go."""
    category_id = category_id_by_name(conn, name)
    with transaction(conn):
        # ON DELETE SET NULL clears category_id; reset the provenance column as well
        conn.execute(
            "UPDATE transactions SET categorized_by = NULL, rule_id = NULL WHERE category_id = ?",
            (category_id,),
        )
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))


# --------------------------------------------------------------------------------------
# Manual insert
# --------------------------------------------------------------------------------------


def add_transaction(
    conn: sqlite3.Connection,
    *,
    txn_date: date,
    description: str,
    amount_paise: int,
    direction: str = "debit",
    category: str | None = None,
    notes: str | None = None,
) -> int:
    """Insert one manual transaction and return its id.

    Without an explicit category the rules engine is applied, exactly as on import.
    A manual entry is always inserted, even if an identical row exists (you really
    did buy two coffees), so the dedupe occurrence counter is bumped until the hash
    is free.
    """
    if amount_paise <= 0:
        raise ValueError("amount must be positive")
    if direction not in ("debit", "credit"):
        raise ValueError("direction must be 'debit' or 'credit'")
    description = normalize_description(description)
    if not description:
        raise ValueError("description must not be empty")

    rule_id = None
    if category is not None:
        category_id: int | None = category_id_by_name(conn, category)
        categorized_by: str | None = "manual"
    else:
        rule = match(load_rules(conn), description)
        category_id = rule.category_id if rule else None
        rule_id = rule.id if rule else None
        categorized_by = "rule" if rule else None

    signed = -amount_paise if direction == "debit" else amount_paise
    with transaction(conn):
        occurrence = 0
        while True:
            digest = dedupe_key(txn_date, signed, description, occurrence)
            exists = conn.execute(
                "SELECT 1 FROM transactions WHERE dedupe_hash = ?", (digest,)
            ).fetchone()
            if not exists:
                break
            occurrence += 1
        cur = conn.execute(
            """
            INSERT INTO transactions
                (txn_date, description, amount_paise, direction, category_id, rule_id,
                 categorized_by, source, dedupe_hash, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)
            """,
            (
                txn_date.isoformat(),
                description,
                amount_paise,
                direction,
                category_id,
                rule_id,
                categorized_by,
                digest,
                notes,
            ),
        )
    return int(cur.lastrowid)


def delete_transaction(conn: sqlite3.Connection, txn_id: int) -> bool:
    return conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,)).rowcount == 1


# --------------------------------------------------------------------------------------
# Retrieval with filters
# --------------------------------------------------------------------------------------


@dataclass
class TxnFilter:
    month: str | None = None
    date_from: str | None = None  # inclusive ISO date
    date_to: str | None = None  # inclusive ISO date
    category: str | None = None
    uncategorized: bool = False
    search: str | None = None
    min_paise: int | None = None
    max_paise: int | None = None
    direction: str | None = None
    limit: int | None = 50
    newest_first: bool = True


def list_transactions(conn: sqlite3.Connection, flt: TxnFilter) -> list[sqlite3.Row]:
    """Relational retrieval: transactions joined to their category, filtered in SQL.

    All values are bound parameters; only fixed SQL fragments are concatenated.
    """
    clauses: list[str] = []
    params: list[object] = []
    if flt.month:
        start, end = month_bounds(flt.month)
        clauses.append("t.txn_date >= ? AND t.txn_date < ?")
        params += [start, end]
    if flt.date_from:
        clauses.append("t.txn_date >= ?")
        params.append(date.fromisoformat(flt.date_from).isoformat())
    if flt.date_to:
        clauses.append("t.txn_date <= ?")
        params.append(date.fromisoformat(flt.date_to).isoformat())
    if flt.category:
        clauses.append("t.category_id = ?")
        params.append(category_id_by_name(conn, flt.category))
    if flt.uncategorized:
        clauses.append("t.category_id IS NULL")
    if flt.search:
        clauses.append("t.description LIKE ? ESCAPE '\\'")
        escaped = flt.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    if flt.min_paise is not None:
        clauses.append("t.amount_paise >= ?")
        params.append(flt.min_paise)
    if flt.max_paise is not None:
        clauses.append("t.amount_paise <= ?")
        params.append(flt.max_paise)
    if flt.direction:
        clauses.append("t.direction = ?")
        params.append(flt.direction)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "DESC" if flt.newest_first else "ASC"
    sql = f"""
        SELECT t.id, t.txn_date, t.description, t.amount_paise, t.direction,
               t.categorized_by, t.source, c.name AS category, c.kind AS category_kind
        FROM transactions AS t
        LEFT JOIN categories AS c ON c.id = t.category_id
        {where}
        ORDER BY t.txn_date {order}, t.id {order}
    """
    if flt.limit is not None:
        sql += " LIMIT ?"
        params.append(int(flt.limit))
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------------------
# Aggregations
# --------------------------------------------------------------------------------------

MONTHLY_TOTALS_SQL = f"""
SELECT substr(t.txn_date, 1, 7) AS month,
       SUM(CASE WHEN {SPEND_FILTER} THEN t.amount_paise ELSE 0 END)  AS spent_paise,
       SUM(CASE WHEN {INCOME_FILTER} THEN t.amount_paise ELSE 0 END) AS income_paise,
       COUNT(*) AS txn_count
FROM transactions AS t {{hint}}
LEFT JOIN categories AS c ON c.id = t.category_id
WHERE t.txn_date >= ? AND t.txn_date < ?
GROUP BY month
ORDER BY month
"""

CATEGORY_TOTALS_SQL = f"""
SELECT t.category_id, COALESCE(c.name, 'Uncategorized') AS category,
       SUM(t.amount_paise) AS spent_paise, COUNT(*) AS txn_count
FROM transactions AS t {{hint}}
LEFT JOIN categories AS c ON c.id = t.category_id
WHERE t.txn_date >= ? AND t.txn_date < ? AND {SPEND_FILTER}
GROUP BY t.category_id
ORDER BY spent_paise DESC, category
"""

DAILY_TOTALS_SQL = f"""
SELECT t.txn_date AS day, SUM(t.amount_paise) AS spent_paise, COUNT(*) AS txn_count
FROM transactions AS t {{hint}}
LEFT JOIN categories AS c ON c.id = t.category_id
WHERE t.txn_date >= ? AND t.txn_date < ? AND {SPEND_FILTER}
GROUP BY t.txn_date
ORDER BY t.txn_date
"""

BUDGET_STATUS_SQL = """
SELECT b.category_id, c.name AS category, b.amount_paise AS budget_paise,
       COALESCE(SUM(t.amount_paise), 0) AS spent_paise
FROM budgets AS b
JOIN categories AS c ON c.id = b.category_id
LEFT JOIN transactions AS t {hint}
       ON t.category_id = b.category_id
      AND t.direction = 'debit'
      AND t.txn_date >= ? AND t.txn_date < ?
WHERE b.month = ?
GROUP BY b.category_id
ORDER BY c.name
"""

CATEGORY_MONTH_LIST_SQL = """
SELECT t.id, t.txn_date, t.description, t.amount_paise
FROM transactions AS t {hint}
WHERE t.category_id = ? AND t.txn_date >= ? AND t.txn_date < ?
ORDER BY t.txn_date
"""

DEDUPE_PROBE_SQL = """
SELECT 1 FROM transactions AS t {hint} WHERE t.dedupe_hash = ?
"""


def _sql(template: str, hint: str = "") -> str:
    return template.replace("{hint}", hint)


@dataclass(frozen=True)
class MonthTotal:
    month: str
    spent_paise: int
    income_paise: int
    txn_count: int

    @property
    def net_paise(self) -> int:
        return self.income_paise - self.spent_paise


def monthly_totals(conn: sqlite3.Connection, first: str, last: str) -> list[MonthTotal]:
    """Spending and income per month for ``first..last`` inclusive; empty months are 0."""
    start, _ = month_bounds(first)
    _, end = month_bounds(last)
    found = {row["month"]: row for row in conn.execute(_sql(MONTHLY_TOTALS_SQL), (start, end))}
    out = []
    for month in month_range(first, last):
        row = found.get(month)
        out.append(
            MonthTotal(
                month=month,
                spent_paise=int(row["spent_paise"]) if row else 0,
                income_paise=int(row["income_paise"]) if row else 0,
                txn_count=int(row["txn_count"]) if row else 0,
            )
        )
    return out


@dataclass(frozen=True)
class CategoryTotal:
    category_id: int | None
    category: str
    spent_paise: int
    txn_count: int


def category_totals(conn: sqlite3.Connection, month: str) -> list[CategoryTotal]:
    start, end = month_bounds(month)
    return [
        CategoryTotal(r["category_id"], r["category"], int(r["spent_paise"]), int(r["txn_count"]))
        for r in conn.execute(_sql(CATEGORY_TOTALS_SQL), (start, end))
    ]


def daily_totals(conn: sqlite3.Connection, month: str) -> list[tuple[date, int]]:
    """Spending for every calendar day of the month (days without spending are 0)."""
    start, end = month_bounds(month)
    found = {
        r["day"]: int(r["spent_paise"]) for r in conn.execute(_sql(DAILY_TOTALS_SQL), (start, end))
    }
    first = date.fromisoformat(start)
    days = calendar.monthrange(first.year, first.month)[1]
    return [(d, found.get(d.isoformat(), 0)) for d in (first + timedelta(n) for n in range(days))]


# --------------------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------------------

WARN_THRESHOLD_PCT = 80.0


@dataclass(frozen=True)
class BudgetStatus:
    category: str
    spent_paise: int
    budget_paise: int | None

    @property
    def pct_used(self) -> float | None:
        if not self.budget_paise:
            return None
        return self.spent_paise * 100.0 / self.budget_paise

    @property
    def remaining_paise(self) -> int | None:
        return None if self.budget_paise is None else self.budget_paise - self.spent_paise

    @property
    def over_budget(self) -> bool:
        return self.budget_paise is not None and self.spent_paise > self.budget_paise

    @property
    def status(self) -> str:
        """``OVER`` (> 100%), ``WARN`` (>= 80%), ``OK``, or ``-`` when there is no budget."""
        pct = self.pct_used
        if pct is None:
            return "-"
        if self.over_budget:
            return "OVER"
        if pct >= WARN_THRESHOLD_PCT:
            return "WARN"
        return "OK"


def set_budget(
    conn: sqlite3.Connection, category: str, amount_paise: int, months: list[str]
) -> int:
    """Upsert a monthly budget for one category over one or more months."""
    if amount_paise <= 0:
        raise ValueError("budget must be positive")
    category_id = category_id_by_name(conn, category)
    kind = conn.execute("SELECT kind FROM categories WHERE id = ?", (category_id,)).fetchone()[0]
    if kind != "expense":
        raise RuleError(f"budgets apply to expense categories; {category!r} is {kind!r}")
    rows = [(parse_month(m), category_id, amount_paise) for m in months]
    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO budgets (month, category_id, amount_paise) VALUES (?, ?, ?)
            ON CONFLICT (month, category_id) DO UPDATE SET amount_paise = excluded.amount_paise
            """,
            rows,
        )
    return len(rows)


def delete_budget(conn: sqlite3.Connection, category: str, month: str) -> bool:
    category_id = category_id_by_name(conn, category)
    cur = conn.execute(
        "DELETE FROM budgets WHERE month = ? AND category_id = ?", (parse_month(month), category_id)
    )
    return cur.rowcount == 1


def budget_status(conn: sqlite3.Connection, month: str) -> list[BudgetStatus]:
    """Budgeted categories for the month with their actual spending (index-backed join)."""
    start, end = month_bounds(month)
    rows = conn.execute(_sql(BUDGET_STATUS_SQL), (start, end, parse_month(month))).fetchall()
    return [
        BudgetStatus(r["category"], int(r["spent_paise"]), int(r["budget_paise"])) for r in rows
    ]


def summary_rows(conn: sqlite3.Connection, month: str) -> list[BudgetStatus]:
    """Every category with spending or a budget this month, budgeted ones first by % used."""
    by_name = {b.category: b for b in budget_status(conn, month)}
    for total in category_totals(conn, month):
        if total.category not in by_name:
            by_name[total.category] = BudgetStatus(total.category, total.spent_paise, None)
    return sorted(
        by_name.values(),
        key=lambda s: (s.budget_paise is None, -(s.pct_used or 0.0), -s.spent_paise, s.category),
    )


@dataclass(frozen=True)
class MonthOverview:
    month: str
    spent_paise: int
    income_paise: int
    txn_count: int
    prev_spent_paise: int
    budget_total_paise: int
    budgeted_spent_paise: int

    @property
    def net_paise(self) -> int:
        return self.income_paise - self.spent_paise

    @property
    def change_vs_prev_pct(self) -> float | None:
        if not self.prev_spent_paise:
            return None
        return (self.spent_paise - self.prev_spent_paise) * 100.0 / self.prev_spent_paise


def month_overview(conn: sqlite3.Connection, month: str) -> MonthOverview:
    prev, cur = monthly_totals(conn, add_months(month, -1), month)
    budgets = budget_status(conn, month)
    return MonthOverview(
        month=month,
        spent_paise=cur.spent_paise,
        income_paise=cur.income_paise,
        txn_count=cur.txn_count,
        prev_spent_paise=prev.spent_paise,
        budget_total_paise=sum(b.budget_paise or 0 for b in budgets),
        budgeted_spent_paise=sum(b.spent_paise for b in budgets),
    )


def list_budgets(conn: sqlite3.Connection, month: str | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT b.month, c.name AS category, b.amount_paise
        FROM budgets b JOIN categories c ON c.id = b.category_id
    """
    params: tuple[str, ...] = ()
    if month:
        sql += " WHERE b.month = ?"
        params = (parse_month(month),)
    return conn.execute(sql + " ORDER BY b.month, c.name", params).fetchall()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.id, b.source_name, b.imported_at, b.rows_read, b.rows_inserted,
               b.rows_duplicate, b.rows_rejected,
               (SELECT COUNT(*) FROM transactions t WHERE t.batch_id = b.id) AS rows_present
        FROM import_batches b ORDER BY b.id
        """
    ).fetchall()
