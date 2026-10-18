from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest

from expense_dashboard import db
from expense_dashboard import queries as q
from expense_dashboard.defaults import seed_defaults


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator:
    """A migrated database with the default categories and rules."""
    c = db.connect(db_path)
    db.migrate(c)
    seed_defaults(c)
    yield c
    c.close()


@pytest.fixture
def bare_conn(db_path: Path) -> Iterator:
    """A migrated database with no categories or rules."""
    c = db.connect(db_path)
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def write_csv(tmp_path: Path) -> Callable[..., Path]:
    def _write(name: str, text: str, encoding: str = "utf-8") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding=encoding, newline="")
        return path

    return _write


@pytest.fixture
def ledger(conn):
    """A small, fully hand-checkable ledger (amounts in rupees).

    Jan 2026: Groceries 1,000.00 (debit) | Food Delivery 250.50 (debit)
              Investments 5,000.00 (debit, transfer - NOT spending)
              Income 50,000.00 (credit)
    Feb 2026: nothing
    Mar 2026: Groceries 300.00 (debit) | uncategorised 199.99 (debit)
              Food Delivery refund 100.00 (credit to an expense category - NOT income)

    Budgets:  Jan Groceries 1,200 | Jan Food Delivery 200 | Mar Groceries 300
    """
    rows = [
        (date(2026, 1, 1), "Monthly pay", 50_000_00, "credit", "Income"),
        (date(2026, 1, 5), "Weekly veggies", 1_000_00, "debit", "Groceries"),
        (date(2026, 1, 20), "Dinner order", 250_50, "debit", "Food Delivery"),
        (date(2026, 1, 31), "Index fund", 5_000_00, "debit", "Investments"),
        (date(2026, 3, 10), "Milk and bread", 300_00, "debit", "Groceries"),
        (date(2026, 3, 10), "MYSTERY SHOP 42", 199_99, "debit", None),
        (date(2026, 3, 15), "Order refund", 100_00, "credit", "Food Delivery"),
    ]
    for d, desc, paise, direction, category in rows:
        q.add_transaction(
            conn,
            txn_date=d,
            description=desc,
            amount_paise=paise,
            direction=direction,
            category=category,
        )
    q.set_budget(conn, "Groceries", 1_200_00, ["2026-01"])
    q.set_budget(conn, "Food Delivery", 200_00, ["2026-01"])
    q.set_budget(conn, "Groceries", 300_00, ["2026-03"])
    return conn
