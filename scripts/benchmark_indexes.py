"""Measure what batching and the indexes buy, at a realistic-to-large scale.

    python scripts/benchmark_indexes.py --rows 200000

1. Compares three insert strategies on the same rows: one autocommitted
   ``execute()`` per row, an ``execute()`` loop inside one transaction, and a single
   ``executemany()`` inside one transaction (what the importer does).
2. Builds a throwaway database with N random transactions spread over ten years,
   then times every query from ``expense explain`` with its indexes versus forced
   ``NOT INDEXED`` full scans.

The rows are random, not realistic spending; only the access pattern matters here.
Timings depend on the machine and disk; quote them as "on my laptop".
"""

from __future__ import annotations

import argparse
import hashlib
import random
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from expense_dashboard import db
from expense_dashboard import explain as ex
from expense_dashboard import queries as q
from expense_dashboard.defaults import seed_defaults
from expense_dashboard.tables import render_table

INSERT_SQL = (
    "INSERT INTO transactions (txn_date, description, amount_paise, direction, "
    "category_id, source, dedupe_hash) VALUES (?, ?, ?, ?, ?, 'manual', ?)"
)


def make_params(conn, rows: int, seed: int) -> list[tuple]:
    rng = random.Random(seed)
    category_ids = [r[0] for r in conn.execute("SELECT id FROM categories WHERE kind='expense'")]
    start = date(2016, 10, 1)
    span = (date(2026, 9, 30) - start).days
    params = []
    for i in range(rows):
        d = start + timedelta(days=rng.randrange(span + 1))
        params.append(
            (
                d.isoformat(),
                f"TXN {i}",
                rng.randint(1_000, 5_00_000),
                "debit" if rng.random() < 0.95 else "credit",
                rng.choice(category_ids),
                hashlib.sha256(f"{seed}-{i}".encode()).hexdigest(),
            )
        )
    return params


def fresh_db(path: Path):
    conn = db.connect(path)
    db.migrate(conn)
    seed_defaults(conn)
    return conn


def compare_inserts(tmp: Path, rows: int) -> list[list[str]]:
    """Time three ways of inserting the same rows into an empty, fully indexed table."""

    def autocommit_loop(conn, params):
        for p in params:  # every statement is its own transaction: one fsync per row
            conn.execute(INSERT_SQL, p)

    def loop_in_transaction(conn, params):
        with db.transaction(conn):
            for p in params:
                conn.execute(INSERT_SQL, p)

    def executemany_in_transaction(conn, params):
        with db.transaction(conn):
            conn.executemany(INSERT_SQL, params)

    out = []
    for name, fn in [
        ("execute() per row, autocommit", autocommit_loop),
        ("execute() loop, one transaction", loop_in_transaction),
        ("executemany(), one transaction", executemany_in_transaction),
    ]:
        conn = fresh_db(tmp / f"insert-{fn.__name__}.db")
        try:
            params = make_params(conn, rows, seed=99)
            t0 = time.perf_counter()
            fn(conn, params)
            elapsed = time.perf_counter() - t0
        finally:
            conn.close()
        out.append([name, f"{rows:,}", f"{elapsed:.2f}", f"{rows / elapsed:,.0f}"])
    return out


def populate(conn, rows: int, seed: int) -> float:
    params = make_params(conn, rows, seed)
    category_ids = sorted({p[4] for p in params})
    t0 = time.perf_counter()
    with db.transaction(conn):
        conn.executemany(INSERT_SQL, params)
    elapsed = time.perf_counter() - t0
    for cat in category_ids[:12]:
        name = conn.execute("SELECT name FROM categories WHERE id = ?", (cat,)).fetchone()[0]
        q.set_budget(conn, name, 5_000_00, q.month_range("2016-10", "2026-09"))
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--repeat", type=int, default=15)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--insert-rows", type=int, default=2_000, help="rows for the insert-strategy comparison"
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        print(f"Insert strategies ({args.insert_rows:,} rows into an empty indexed table):")
        print(
            render_table(
                ["Strategy", "Rows", "Seconds", "Rows/s"],
                compare_inserts(Path(tmp), args.insert_rows),
                align="lrrr",
            )
        )
        print()
        conn = fresh_db(Path(tmp) / "bench.db")
        try:
            load_s = populate(conn, args.rows, args.seed)
            print(
                f"bulk-inserted {args.rows:,} rows with executemany in {load_s:.2f}s "
                f"({args.rows / load_s:,.0f} rows/s)\n"
            )
            table = []
            for query in ex.explain_queries(conn):
                plan = ex.explain(conn, query)
                b = ex.benchmark(conn, query, repeat=args.repeat)
                table.append(
                    [
                        b.name,
                        "yes" if plan.uses_expected_index else "NO",
                        f"{b.indexed_ms:.2f}",
                        f"{b.full_scan_ms:.2f}",
                        f"{b.speedup:.1f}x",
                    ]
                )
            print(
                render_table(
                    ["Query", "Index used", "Indexed ms", "NOT INDEXED ms", "Speed-up"],
                    table,
                    align="llrrr",
                )
            )
        finally:
            conn.close()


if __name__ == "__main__":
    main()
