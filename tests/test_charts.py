from __future__ import annotations

from datetime import date

import matplotlib
import pytest

from expense_dashboard import charts
from expense_dashboard import queries as q

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _assert_png(path, min_bytes=10_000):
    data = path.read_bytes()
    assert data.startswith(PNG_MAGIC), path
    assert len(data) > min_bytes, f"{path} is suspiciously small ({len(data)} bytes)"


def test_backend_is_agg():
    assert matplotlib.get_backend().lower() == "agg"


def test_generate_report_writes_four_pngs(ledger, tmp_path):
    paths = charts.generate_report(ledger, tmp_path / "out", "2026-03", months=6, note="test")
    assert [p.name for p in paths] == list(charts.REPORT_FILES)
    for p in paths:
        _assert_png(p)


def test_report_png_dimensions(ledger, tmp_path):
    from PIL import Image  # pillow is a matplotlib dependency

    (trend, *_rest) = charts.generate_report(ledger, tmp_path, "2026-01", months=3)
    with Image.open(trend) as img:
        assert img.size == (10 * charts.DPI, int(4.8 * charts.DPI))


def test_charts_handle_empty_month_and_no_budgets(conn, tmp_path):
    # no transactions at all: every chart still renders (with an explanatory message)
    paths = charts.generate_report(conn, tmp_path, "2026-05", months=1)
    for p in paths:
        _assert_png(p, min_bytes=3_000)


def test_category_breakdown_folds_tail_into_other(tmp_path, monkeypatch):
    totals = [q.CategoryTotal(i, f"Cat {i}", (20 - i) * 100_00, 1) for i in range(14)]
    captured = {}
    real_barh = matplotlib.axes.Axes.barh

    def spy(self, y, width, *args, **kwargs):
        captured["widths"] = list(width)
        return real_barh(self, y, width, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "barh", spy)
    path = charts.category_breakdown_chart(totals, "2026-01", tmp_path / "c.png", top_n=10)
    _assert_png(path)
    assert len(captured["widths"]) == 11  # top 10 + "Other (4)"
    assert captured["widths"][-1] == pytest.approx(sum(20 - i for i in range(10, 14)) * 100)


def test_budget_chart_colours_follow_status(tmp_path, monkeypatch):
    statuses = [
        q.BudgetStatus("A", 50, 100),
        q.BudgetStatus("B", 90, 100),
        q.BudgetStatus("C", 150, 100),
        q.BudgetStatus("D", 10, None),
    ]
    captured = {}
    real_barh = matplotlib.axes.Axes.barh

    def spy(self, y, width, *args, **kwargs):
        captured["widths"], captured["colors"] = list(width), kwargs["color"]
        return real_barh(self, y, width, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "barh", spy)
    charts.budget_adherence_chart(statuses, "2026-01", tmp_path / "b.png")
    # sorted by % used, unbudgeted category dropped
    assert captured["widths"] == [150.0, 90.0, 50.0]
    assert captured["colors"] == [charts.STATUS["OVER"], charts.STATUS["WARN"], charts.STATUS["OK"]]


def test_daily_chart_cumulative_line(tmp_path, monkeypatch):
    daily = [(date(2026, 2, d), d * 100) for d in range(1, 29)]
    lines = []
    real_plot = matplotlib.axes.Axes.plot

    def spy(self, x, y, *args, **kwargs):
        lines.append((kwargs.get("label"), list(y)))
        return real_plot(self, x, y, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "plot", spy)
    charts.daily_spending_chart(daily, "2026-02", tmp_path / "d.png", budget_total_paise=28_00)
    cumulative = dict(lines)["Cumulative spend"]
    assert cumulative[-1] == pytest.approx(sum(range(1, 29)))
    pace = dict(lines)["Even budget pace"]
    assert pace[-1] == pytest.approx(28.0)


def test_generate_report_rejects_bad_window(ledger, tmp_path):
    with pytest.raises(ValueError):
        charts.generate_report(ledger, tmp_path, "2026-01", months=0)
