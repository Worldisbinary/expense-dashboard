"""EXPLAIN QUERY PLAN assertions: the indexes in the migration are really used."""

from __future__ import annotations

import pytest

from expense_dashboard import explain as ex


@pytest.fixture
def populated(conn, tmp_path):
    from expense_dashboard.demo import load_demo

    load_demo(conn, tmp_path / "data", seed=7, end_month="2026-06", months=3)
    return conn


@pytest.mark.parametrize("use_data", [False, True], ids=["empty-db", "with-data"])
def test_every_query_uses_its_expected_index(request, conn, use_data):
    db = request.getfixturevalue("populated") if use_data else conn
    results = ex.explain_all(db)
    assert {r.query.name for r in results} == {
        "monthly_trend",
        "category_totals",
        "daily_totals",
        "budget_adherence",
        "category_month_list",
        "dedupe_probe",
    }
    for result in results:
        assert result.uses_expected_index, (result.query.name, result.details)
        assert result.full_scans == [], (result.query.name, result.details)


def test_aggregates_are_answered_from_covering_indexes(conn):
    """The date-range aggregates never touch the table rows, only the index."""
    for result in ex.explain_all(conn):
        if result.query.name in {
            "monthly_trend",
            "category_totals",
            "daily_totals",
            "budget_adherence",
            "dedupe_probe",
        }:
            assert any("COVERING INDEX" in d for d in result.details), result.details


def test_budget_join_seeks_transactions_by_category_and_date_range(conn):
    (budget,) = [r for r in ex.explain_all(conn) if r.query.name == "budget_adherence"]
    detail = next(d for d in budget.details if "idx_transactions_category_date" in d)
    assert "category_id=?" in detail and "txn_date>?" in detail and "txn_date<?" in detail


def test_dropping_an_index_is_detected(conn):
    """Guards the guard: without the index the planner falls back to a full scan."""
    conn.execute("DROP INDEX idx_transactions_date")
    (daily,) = [r for r in ex.explain_all(conn) if r.query.name == "daily_totals"]
    assert not daily.uses_expected_index
    assert daily.full_scans


def test_not_indexed_hint_forces_full_scan(conn):
    query = next(x for x in ex.explain_queries(conn) if x.name == "dedupe_probe")
    assert ex.explain(conn, query, hint="NOT INDEXED").full_scans


def test_render_tree_indents_children(conn):
    (budget,) = [r for r in ex.explain_all(conn) if r.query.name == "budget_adherence"]
    tree = budget.render_tree()
    assert tree.splitlines()[0].startswith("|-- SEARCH b")


def test_render_tree_nesting():
    result = ex.ExplainResult(
        ex.ExplainQuery("x", "", "", (), "idx"),
        [ex.PlanStep(2, 0, "SCAN a"), ex.PlanStep(5, 2, "SEARCH b USING INDEX idx (k=?)")],
    )
    assert result.render_tree() == "|-- SCAN a\n   |-- SEARCH b USING INDEX idx (k=?)"
    assert result.full_scans == ["SCAN a"]
    assert result.uses_expected_index


def test_benchmark_returns_timings(populated):
    query = next(x for x in ex.explain_queries(populated) if x.name == "budget_adherence")
    result = ex.benchmark(populated, query, repeat=3)
    assert result.indexed_ms > 0 and result.full_scan_ms > 0 and result.speedup > 0
    assert ex.BenchmarkResult("x", 0.0, 1.0).speedup == float("inf")
