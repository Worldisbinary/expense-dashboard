from __future__ import annotations

from expense_dashboard import demo
from expense_dashboard import queries as q


def test_generator_is_deterministic(tmp_path):
    a = demo.write_statements(demo.generate_transactions(1, "2026-03", 2), tmp_path / "a")
    b = demo.write_statements(demo.generate_transactions(1, "2026-03", 2), tmp_path / "b")
    c = demo.write_statements(demo.generate_transactions(2, "2026-03", 2), tmp_path / "c")
    assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]
    assert [p.read_bytes() for p in a] != [p.read_bytes() for p in c]


def test_files_are_labelled_synthetic(tmp_path):
    bank, card = demo.write_statements(demo.generate_transactions(3, "2026-01", 1), tmp_path)
    assert demo.SYNTHETIC_BANNER in bank.read_text(encoding="utf-8-sig").splitlines()[0]
    assert card.read_text(encoding="utf-8").splitlines()[0] == "Transaction Date,Description,Amount"


def test_generated_months_and_content():
    txns = demo.generate_transactions(42, "2026-09", 12)
    months = {t.day.strftime("%Y-%m") for t in txns}
    assert months == set(q.month_range("2025-10", "2026-09"))
    assert all(t.paise > 0 for t in txns)
    text = " ".join(t.narration for t in txns)
    for merchant in ("SWIGGY", "ZOMATO", "HOUSE RENT", "BLINKIT", "FUEL", "NETFLIX", "UPI/"):
        assert merchant in text
    salaries = [t for t in txns if "SALARY" in t.narration]
    assert len(salaries) == 12 and all(not t.is_debit for t in salaries)
    # April appraisal is a step up and never reverts
    amounts = [t.paise for t in sorted(salaries, key=lambda t: t.day)]
    assert amounts == sorted(amounts)


def test_card_bill_payments_balance(tmp_path):
    txns = demo.generate_transactions(5, "2026-04", 3)
    paid = [t.paise for t in txns if "CREDIT CARD PAYMENT" in t.narration]
    received = [t.paise for t in txns if t.narration.startswith("PAYMENT RECEIVED")]
    assert paid == received and len(paid) == 2  # last month's bill falls after the window


def test_load_demo_runs_the_real_pipeline(conn, tmp_path):
    results = demo.load_demo(conn, tmp_path, seed=42, end_month="2026-09", months=12)
    assert all(r.rows_rejected == 0 and r.rows_inserted > 0 for r in results)
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert total == sum(r.rows_inserted for r in results) > 600
    categorised = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NOT NULL"
    ).fetchone()[0]
    # default rules catch most narrations; tea-stall and friend UPI payments are left
    # uncategorised on purpose (about 21% of rows with seed 42)
    assert 0.7 < categorised / total < 0.9
    assert len(q.list_budgets(conn, "2026-09")) == len(demo.DEMO_BUDGETS)
    # card bill payments are transfers: they must not inflate spending
    spent = q.monthly_totals(conn, "2026-09", "2026-09")[0].spent_paise
    gross_debits = conn.execute(
        "SELECT SUM(amount_paise) FROM transactions WHERE direction = 'debit' "
        "AND txn_date LIKE '2026-09-%'"
    ).fetchone()[0]
    assert 0 < spent < gross_debits
