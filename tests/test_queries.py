"""Aggregation and budget maths against hand-computed values (see the ``ledger`` fixture)."""

from __future__ import annotations

from datetime import date

import pytest

from expense_dashboard import queries as q
from expense_dashboard.categorize import RuleError

# ------------------------------------------------------------------ month helpers


@pytest.mark.parametrize(
    ("month", "bounds"),
    [
        ("2026-01", ("2026-01-01", "2026-02-01")),
        ("2026-02", ("2026-02-01", "2026-03-01")),
        ("2024-02", ("2024-02-01", "2024-03-01")),
        ("2026-12", ("2026-12-01", "2027-01-01")),
    ],
)
def test_month_bounds_half_open(month, bounds):
    assert q.month_bounds(month) == bounds


@pytest.mark.parametrize("bad", ["2026-13", "2026-1", "26-01", "", "2026-00"])
def test_parse_month_rejects(bad):
    with pytest.raises(ValueError):
        q.parse_month(bad)


def test_add_months_and_range():
    assert q.add_months("2026-01", -1) == "2025-12"
    assert q.add_months("2025-11", 14) == "2027-01"
    assert q.month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert q.month_range("2026-03", "2026-01") == []


def test_latest_month(conn, ledger):
    assert q.latest_month(conn) == "2026-03"


def test_latest_month_empty(conn):
    assert q.latest_month(conn) is None


# ------------------------------------------------------------------ aggregation


def test_monthly_totals_hand_computed(ledger):
    totals = q.monthly_totals(ledger, "2025-12", "2026-03")
    assert [(t.month, t.spent_paise, t.income_paise, t.txn_count) for t in totals] == [
        ("2025-12", 0, 0, 0),  # empty months are filled in, not skipped
        # 1,000.00 groceries + 250.50 food; the 5,000 SIP is a transfer, not spending
        ("2026-01", 1_250_50, 50_000_00, 4),
        ("2026-02", 0, 0, 0),
        # 300.00 + 199.99 uncategorised; the 100.00 refund is not income
        ("2026-03", 499_99, 0, 3),
    ]
    assert totals[1].net_paise == 50_000_00 - 1_250_50


def test_category_totals_hand_computed(ledger):
    jan = q.category_totals(ledger, "2026-01")
    assert [(c.category, c.spent_paise, c.txn_count) for c in jan] == [
        ("Groceries", 1_000_00, 1),
        ("Food Delivery", 250_50, 1),
    ]
    mar = q.category_totals(ledger, "2026-03")
    assert [(c.category, c.spent_paise) for c in mar] == [
        ("Groceries", 300_00),
        ("Uncategorized", 199_99),
    ]
    assert mar[1].category_id is None
    assert q.category_totals(ledger, "2026-02") == []


def test_daily_totals_fill_every_day(ledger):
    days = q.daily_totals(ledger, "2026-03")
    assert len(days) == 31
    assert days[0] == (date(2026, 3, 1), 0)
    assert dict(days)[date(2026, 3, 10)] == 300_00 + 199_99
    assert sum(p for _, p in days) == 499_99
    assert len(q.daily_totals(ledger, "2026-02")) == 28


def test_aggregates_see_new_rows_immediately(ledger):
    q.add_transaction(
        ledger,
        txn_date=date(2026, 2, 14),
        description="Flowers",
        amount_paise=799_00,
        category="Shopping",
    )
    assert q.monthly_totals(ledger, "2026-02", "2026-02")[0].spent_paise == 799_00


# ------------------------------------------------------------------ budgets


def test_budget_status_math(ledger):
    jan = {s.category: s for s in q.budget_status(ledger, "2026-01")}
    groceries, food = jan["Groceries"], jan["Food Delivery"]

    assert groceries.spent_paise == 1_000_00 and groceries.budget_paise == 1_200_00
    assert groceries.pct_used == pytest.approx(83.3333, rel=1e-4)
    assert groceries.remaining_paise == 200_00
    assert groceries.status == "WARN" and not groceries.over_budget

    assert food.pct_used == pytest.approx(125.25)
    assert food.remaining_paise == -50_50
    assert food.status == "OVER" and food.over_budget


def test_exactly_at_budget_is_warn_not_over(ledger):
    (mar,) = q.budget_status(ledger, "2026-03")
    assert (mar.pct_used, mar.remaining_paise, mar.status) == (100.0, 0, "WARN")


@pytest.mark.parametrize(
    ("spent", "budget", "status"),
    [
        (0, 100, "OK"),
        (79_99, 100_00, "OK"),
        (80_00, 100_00, "WARN"),
        (100_01, 100_00, "OVER"),
        (5, None, "-"),
    ],
)
def test_status_thresholds(spent, budget, status):
    s = q.BudgetStatus("X", spent, budget)
    assert s.status == status
    if budget is None:
        assert s.pct_used is None and s.remaining_paise is None and not s.over_budget


def test_budget_with_no_spending(ledger):
    q.set_budget(ledger, "Fuel", 3_000_00, ["2026-02"])
    (feb,) = q.budget_status(ledger, "2026-02")
    assert (feb.category, feb.spent_paise, feb.pct_used, feb.status) == ("Fuel", 0, 0.0, "OK")


def test_set_budget_upserts_and_spans_months(conn):
    assert q.set_budget(conn, "Fuel", 2_000_00, q.month_range("2026-01", "2026-03")) == 3
    q.set_budget(conn, "Fuel", 2_500_00, ["2026-02"])  # update, not duplicate
    rows = [(r["month"], r["amount_paise"]) for r in q.list_budgets(conn)]
    assert rows == [("2026-01", 2_000_00), ("2026-02", 2_500_00), ("2026-03", 2_000_00)]
    assert [r["month"] for r in q.list_budgets(conn, "2026-02")] == ["2026-02"]


def test_set_budget_validation(conn):
    with pytest.raises(RuleError, match="expense categories"):
        q.set_budget(conn, "Investments", 100, ["2026-01"])
    with pytest.raises(ValueError):
        q.set_budget(conn, "Fuel", 0, ["2026-01"])
    with pytest.raises(ValueError):
        q.set_budget(conn, "Fuel", 100, ["2026-13"])
    with pytest.raises(RuleError, match="unknown category"):
        q.set_budget(conn, "Yachts", 100, ["2026-01"])


def test_delete_budget(conn):
    q.set_budget(conn, "Fuel", 100, ["2026-01"])
    assert q.delete_budget(conn, "Fuel", "2026-01")
    assert not q.delete_budget(conn, "Fuel", "2026-01")


def test_summary_rows_order_and_union(ledger):
    rows = q.summary_rows(ledger, "2026-01")
    # budgeted first, highest % used first; unbudgeted categories with spending after
    assert [(r.category, r.status) for r in rows] == [
        ("Food Delivery", "OVER"),
        ("Groceries", "WARN"),
    ]
    rows = q.summary_rows(ledger, "2026-03")
    assert [(r.category, r.budget_paise) for r in rows] == [
        ("Groceries", 300_00),
        ("Uncategorized", None),
    ]


def test_month_overview(ledger):
    ov = q.month_overview(ledger, "2026-03")
    assert (ov.spent_paise, ov.income_paise, ov.txn_count) == (499_99, 0, 3)
    assert ov.prev_spent_paise == 0 and ov.change_vs_prev_pct is None
    assert (ov.budget_total_paise, ov.budgeted_spent_paise) == (300_00, 300_00)
    jan = q.month_overview(ledger, "2026-01")
    assert jan.net_paise == 50_000_00 - 1_250_50
    q.add_transaction(ledger, txn_date=date(2026, 2, 1), description="x", amount_paise=1_000_00)
    feb = q.month_overview(ledger, "2026-02")
    assert feb.change_vs_prev_pct == pytest.approx((1_000_00 - 1_250_50) * 100 / 1_250_50)


# ------------------------------------------------------------------ retrieval


def _descriptions(conn, **kwargs):
    return [r["description"] for r in q.list_transactions(conn, q.TxnFilter(**kwargs))]


def test_list_filters(ledger):
    assert len(_descriptions(ledger, limit=None)) == 7
    assert _descriptions(ledger, month="2026-03", newest_first=False) == [
        "Milk and bread",
        "MYSTERY SHOP 42",
        "Order refund",
    ]
    assert _descriptions(ledger, category="groceries") == ["Milk and bread", "Weekly veggies"]
    assert _descriptions(ledger, uncategorized=True) == ["MYSTERY SHOP 42"]
    assert _descriptions(ledger, search="order") == ["Order refund", "Dinner order"]
    assert _descriptions(ledger, min_paise=5_000_00) == ["Index fund", "Monthly pay"]
    assert _descriptions(ledger, max_paise=200_00, direction="debit") == ["MYSTERY SHOP 42"]
    assert _descriptions(ledger, direction="credit") == ["Order refund", "Monthly pay"]
    assert _descriptions(ledger, date_from="2026-01-20", date_to="2026-01-31") == [
        "Index fund",
        "Dinner order",
    ]
    # same-day rows tie-break on id, newest insert first
    assert _descriptions(ledger, limit=2) == ["Order refund", "MYSTERY SHOP 42"]


def test_list_search_treats_wildcards_literally(conn):
    for desc in ("50% OFF SALE", "500 OFF", "A_B", "AXB"):
        q.add_transaction(conn, txn_date=date(2026, 1, 1), description=desc, amount_paise=1)
    assert _descriptions(conn, search="50%") == ["50% OFF SALE"]
    assert _descriptions(conn, search="A_B") == ["A_B"]


# ------------------------------------------------------------------ manual insert


def test_add_transaction_rules_and_duplicates(conn):
    first = q.add_transaction(
        conn, txn_date=date(2026, 3, 1), description="UPI/ZOMATO/1", amount_paise=250_00
    )
    second = q.add_transaction(
        conn, txn_date=date(2026, 3, 1), description="upi/zomato/1", amount_paise=250_00
    )
    rows = conn.execute(
        "SELECT t.dedupe_hash, c.name, t.source, t.categorized_by FROM transactions t "
        "JOIN categories c ON c.id = t.category_id WHERE t.id IN (?, ?)",
        (first, second),
    ).fetchall()
    assert len({r["dedupe_hash"] for r in rows}) == 2  # both kept, distinct identities
    assert {(r["name"], r["source"], r["categorized_by"]) for r in rows} == {
        ("Food Delivery", "manual", "rule")
    }


@pytest.mark.parametrize(
    "kwargs",
    [{"amount_paise": 0}, {"direction": "up"}, {"description": "   "}],
)
def test_add_transaction_validation(conn, kwargs):
    args = {"txn_date": date(2026, 1, 1), "description": "x", "amount_paise": 1, **kwargs}
    with pytest.raises(ValueError):
        q.add_transaction(conn, **args)


def test_delete_transaction(conn):
    txn = q.add_transaction(conn, txn_date=date(2026, 1, 1), description="x", amount_paise=1)
    assert q.delete_transaction(conn, txn)
    assert not q.delete_transaction(conn, txn)


# ------------------------------------------------------------------ categories


def test_categories_add_list_delete(conn):
    q.add_category(conn, "Pets")
    with pytest.raises(RuleError, match="already exists"):
        q.add_category(conn, "pets")
    txn = q.add_transaction(
        conn, txn_date=date(2026, 1, 1), description="Kibble", amount_paise=1, category="Pets"
    )
    q.set_budget(conn, "Pets", 100, ["2026-01"])
    pets = next(r for r in q.list_categories(conn) if r["name"] == "Pets")
    assert (pets["txn_count"], pets["rule_count"]) == (1, 0)

    q.delete_category(conn, "Pets")
    row = conn.execute(
        "SELECT category_id, categorized_by FROM transactions WHERE id = ?", (txn,)
    ).fetchone()
    assert tuple(row) == (None, None)
    assert q.list_budgets(conn) == []
