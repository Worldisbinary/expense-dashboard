from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from expense_dashboard import db
from expense_dashboard.etl import dedupe_key

HASH = "a" * 64


def _insert_txn(conn, **overrides):
    values = {
        "txn_date": "2026-01-15",
        "description": "test",
        "amount_paise": 100,
        "direction": "debit",
        "source": "manual",
        "dedupe_hash": HASH,
        "category_id": None,
        "batch_id": None,
    }
    values.update(overrides)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return conn.execute(
        f"INSERT INTO transactions ({cols}) VALUES ({marks})", list(values.values())
    )


def test_migrate_creates_all_tables_and_sets_version(bare_conn):
    tables = {
        r[0] for r in bare_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"categories", "transactions", "budgets", "rules", "import_batches"} <= tables
    assert db.schema_version(bare_conn) == len(db.available_migrations())


def test_migrate_is_idempotent(bare_conn):
    assert db.migrate(bare_conn) == []


def test_expected_indexes_exist(bare_conn):
    indexes = {
        r[0] for r in bare_conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {
        "idx_transactions_date",
        "idx_transactions_category_date",
        "ux_transactions_dedupe_hash",
        "idx_transactions_batch",
        "idx_rules_category",
    } <= indexes

    def columns(index):
        return [r["name"] for r in bare_conn.execute(f"PRAGMA index_info({index})")]

    # range column first, then the columns the aggregates read (covering index)
    assert columns("idx_transactions_date") == [
        "txn_date",
        "direction",
        "category_id",
        "amount_paise",
    ]
    # equality column first, range column second, then covering columns
    assert columns("idx_transactions_category_date") == [
        "category_id",
        "txn_date",
        "direction",
        "amount_paise",
    ]


def test_foreign_keys_are_enforced(bare_conn):
    assert bare_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert_txn(bare_conn, category_id=999)


@pytest.mark.parametrize(
    "overrides",
    [
        {"txn_date": "2026-02-30"},  # not a real day
        {"txn_date": "15/01/2026"},  # not ISO
        {"txn_date": "2026-1-5"},  # not canonical
        {"amount_paise": 0},
        {"amount_paise": -500},
        {"direction": "sideways"},
        {"description": "   "},
        {"dedupe_hash": "short"},
        {"source": "import"},  # an imported row must belong to a batch
        {"source": "scraped"},
    ],
)
def test_transaction_check_constraints(bare_conn, overrides):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_txn(bare_conn, **overrides)


def test_dedupe_hash_is_unique(bare_conn):
    _insert_txn(bare_conn)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert_txn(bare_conn, description="other")


def test_category_constraints(bare_conn):
    bare_conn.execute("INSERT INTO categories (name) VALUES ('Food')")
    with pytest.raises(sqlite3.IntegrityError):
        bare_conn.execute("INSERT INTO categories (name) VALUES ('FOOD')")  # NOCASE unique
    with pytest.raises(sqlite3.IntegrityError):
        bare_conn.execute("INSERT INTO categories (name, kind) VALUES ('X', 'luxury')")


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "26-01", "2026-1", "2026/01"])
def test_budget_month_check(bare_conn, month):
    bare_conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Food')")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        bare_conn.execute(
            "INSERT INTO budgets (month, category_id, amount_paise) VALUES (?, 1, 100)", (month,)
        )


def test_budget_amount_must_be_positive_and_unique_per_month(bare_conn):
    bare_conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Food')")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        bare_conn.execute(
            "INSERT INTO budgets (month, category_id, amount_paise) VALUES ('2026-01', 1, 0)"
        )
    bare_conn.execute(
        "INSERT INTO budgets (month, category_id, amount_paise) VALUES ('2026-01', 1, 5)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        bare_conn.execute(
            "INSERT INTO budgets (month, category_id, amount_paise) VALUES ('2026-01', 1, 9)"
        )


def test_batch_counters_must_reconcile(bare_conn):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        bare_conn.execute(
            "INSERT INTO import_batches (source_name, file_sha256, rows_read, rows_inserted) "
            "VALUES ('x.csv', ?, 5, 4)",
            ("b" * 64,),
        )


def test_deleting_a_batch_cascades_to_its_transactions(bare_conn):
    batch = bare_conn.execute(
        "INSERT INTO import_batches (source_name, file_sha256) VALUES ('x.csv', ?)", ("c" * 64,)
    ).lastrowid
    _insert_txn(bare_conn, source="import", batch_id=batch)
    bare_conn.execute("DELETE FROM import_batches WHERE id = ?", (batch,))
    assert bare_conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_deleting_a_category_uncategorises_transactions_and_drops_budgets(bare_conn):
    bare_conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Food')")
    _insert_txn(bare_conn, category_id=1)
    bare_conn.execute(
        "INSERT INTO budgets (month, category_id, amount_paise) VALUES ('2026-01', 1, 5)"
    )
    bare_conn.execute("DELETE FROM categories WHERE id = 1")
    assert bare_conn.execute("SELECT category_id FROM transactions").fetchone()[0] is None
    assert bare_conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 0


def test_transaction_context_manager_rolls_back(bare_conn):
    with pytest.raises(RuntimeError), db.transaction(bare_conn):
        _insert_txn(bare_conn, dedupe_hash=dedupe_key(date(2026, 1, 15), -100, "test"))
        raise RuntimeError("boom")
    assert bare_conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert not bare_conn.in_transaction


def test_require_schema(db_path):
    conn = db.connect(db_path)
    with pytest.raises(db.DatabaseNotInitializedError, match="expense init"):
        db.require_schema(conn)
    db.migrate(conn)
    db.require_schema(conn)  # no error once migrated
    conn.close()


def test_require_schema_detects_outdated_database(db_path, monkeypatch):
    conn = db.connect(db_path)
    db.migrate(conn)
    real = db.available_migrations()
    monkeypatch.setattr(
        db, "available_migrations", lambda: [*real, (len(real) + 1, "9999_future.sql", "")]
    )
    with pytest.raises(db.DatabaseNotInitializedError, match="migrate"):
        db.require_schema(conn)
    conn.close()


def test_failed_migration_leaves_previous_version(db_path, monkeypatch):
    conn = db.connect(db_path)
    db.migrate(conn)
    real = db.available_migrations()
    broken = (len(real) + 1, f"{len(real) + 1:04d}_broken.sql", "CREATE TABLE ok (x);\nNOT SQL;")
    monkeypatch.setattr(db, "available_migrations", lambda: [*real, broken])
    with pytest.raises(sqlite3.Error):
        db.migrate(conn)
    assert db.schema_version(conn) == len(real)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok" not in tables  # the partial DDL was rolled back with the failed migration
    conn.close()


def test_connect_in_memory():
    conn = db.connect(":memory:")
    assert db.migrate(conn) == [name for _, name, _ in db.available_migrations()]
    conn.close()
