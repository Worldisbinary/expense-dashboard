"""``EXPLAIN QUERY PLAN`` for the app's real queries, plus an optional micro-benchmark.

Each entry in :data:`EXPLAIN_QUERIES` is the exact SQL template the app executes
(imported from :mod:`expense_dashboard.queries`) together with representative
parameters and the index we expect SQLite's planner to choose. Tests assert the
expectation, so dropping or reordering an index breaks the build.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from expense_dashboard import queries as q


@dataclass(frozen=True)
class ExplainQuery:
    name: str
    purpose: str
    template: str
    params: tuple
    expected_index: str


def _sample_params(conn: sqlite3.Connection) -> dict[str, tuple]:
    month = q.latest_month(conn) or "2026-01"
    start, end = q.month_bounds(month)
    first_trend = q.add_months(month, -11)
    trend_start, _ = q.month_bounds(first_trend)
    row = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()
    category_id = row[0] if row else 1
    return {
        "range": (start, end),
        "trend": (trend_start, end),
        "budget": (start, end, month),
        "category_month": (category_id, start, end),
        "hash": ("0" * 64,),
    }


def explain_queries(conn: sqlite3.Connection) -> list[ExplainQuery]:
    p = _sample_params(conn)
    return [
        ExplainQuery(
            "monthly_trend",
            "spending/income per month for the trend chart (12-month window)",
            q.MONTHLY_TOTALS_SQL,
            p["trend"],
            "idx_transactions_date",
        ),
        ExplainQuery(
            "category_totals",
            "spending per category for one month (summary, breakdown chart)",
            q.CATEGORY_TOTALS_SQL,
            p["range"],
            "idx_transactions_date",
        ),
        ExplainQuery(
            "daily_totals",
            "spending per day of one month (daily chart)",
            q.DAILY_TOTALS_SQL,
            p["range"],
            "idx_transactions_date",
        ),
        ExplainQuery(
            "budget_adherence",
            "budgets joined to that month's spending in each budgeted category",
            q.BUDGET_STATUS_SQL,
            p["budget"],
            "idx_transactions_category_date",
        ),
        ExplainQuery(
            "category_month_list",
            "list --category X --month M",
            q.CATEGORY_MONTH_LIST_SQL,
            p["category_month"],
            "idx_transactions_category_date",
        ),
        ExplainQuery(
            "dedupe_probe",
            "does a row with this hash already exist? (INSERT OR IGNORE, dry-run import)",
            q.DEDUPE_PROBE_SQL,
            p["hash"],
            "ux_transactions_dedupe_hash",
        ),
    ]


@dataclass(frozen=True)
class PlanStep:
    id: int
    parent: int
    detail: str


@dataclass(frozen=True)
class ExplainResult:
    query: ExplainQuery
    steps: list[PlanStep]

    @property
    def details(self) -> list[str]:
        return [s.detail for s in self.steps]

    @property
    def uses_expected_index(self) -> bool:
        return any(self.query.expected_index in d for d in self.details)

    @property
    def full_scans(self) -> list[str]:
        """Plan steps that read a whole table or a whole index.

        ``SEARCH`` means the planner seeks into a B-tree with a key or range;
        ``SCAN`` means it walks all of it, even when the walk goes through an index
        (``SCAN t USING COVERING INDEX ...``).
        """
        return [d for d in self.details if d.startswith("SCAN ")]

    def render_tree(self) -> str:
        """Indent plan steps by their parent links, like the sqlite3 shell's .eqp output."""
        depth: dict[int, int] = {0: -1}
        lines = []
        for step in self.steps:
            level = depth.get(step.parent, -1) + 1
            depth[step.id] = level
            lines.append(f"{'   ' * level}|-- {step.detail}")
        return "\n".join(lines)


def explain(conn: sqlite3.Connection, query: ExplainQuery, hint: str = "") -> ExplainResult:
    sql = query.template.replace("{hint}", hint)
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", query.params).fetchall()
    return ExplainResult(query, [PlanStep(r[0], r[1], r[3]) for r in rows])


def explain_all(conn: sqlite3.Connection) -> list[ExplainResult]:
    return [explain(conn, query) for query in explain_queries(conn)]


@dataclass(frozen=True)
class BenchmarkResult:
    name: str
    indexed_ms: float
    full_scan_ms: float

    @property
    def speedup(self) -> float:
        return self.full_scan_ms / self.indexed_ms if self.indexed_ms else float("inf")


def benchmark(conn: sqlite3.Connection, query: ExplainQuery, repeat: int = 50) -> BenchmarkResult:
    """Median wall time of the query with its indexes vs. forced ``NOT INDEXED``."""

    def run(hint: str) -> float:
        sql = query.template.replace("{hint}", hint)
        timings = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            conn.execute(sql, query.params).fetchall()
            timings.append((time.perf_counter() - t0) * 1000)
        timings.sort()
        return timings[len(timings) // 2]

    run("")  # warm the page cache so the first variant is not penalised
    return BenchmarkResult(query.name, run(""), run("NOT INDEXED"))
