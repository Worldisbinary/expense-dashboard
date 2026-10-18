"""Matplotlib report charts, rendered headlessly to PNG.

* The ``Agg`` backend is selected before anything else touches matplotlib, so
  reports work over SSH, in CI and in cron jobs where there is no display.
* Charts are built on :class:`matplotlib.figure.Figure` objects directly rather than
  through ``pyplot``: no global figure registry, nothing to ``close()``, no leak
  when a report is generated in a loop.
* Styling follows a small fixed palette: one hue for magnitude, reserved status
  colours (good/warning/critical) that always come with a text label, recessive
  grid lines, and no dual y-axes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator

from expense_dashboard import queries as q
from expense_dashboard.money import RUPEE, format_compact, format_inr

# Palette ------------------------------------------------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES_1 = "#2a78d6"  # blue: spending
SERIES_2 = "#1baf7a"  # aqua: income
STATUS = {"OK": "#0ca30c", "WARN": "#fab219", "OVER": "#d03b3b"}

STYLE = {
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": BASELINE,
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.titlepad": 14,
    "axes.labelsize": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelcolor": INK_2,
    "ytick.labelcolor": INK_2,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.frameon": False,
    "legend.fontsize": 9,
    "font.family": "DejaVu Sans",  # bundled with matplotlib and has the ₹ glyph
    "font.size": 10,
}

DPI = 150

_rupee_axis = FuncFormatter(lambda value, _pos: format_compact(value))


def _new_figure(width: float, height: float, nrows: int = 1, **kwargs) -> tuple[Figure, list]:
    """Create a figure inside an active ``rc_context(STYLE)`` so the style applies."""
    fig = Figure(figsize=(width, height), dpi=DPI, layout="constrained")
    axes = fig.subplots(nrows, 1, squeeze=False, **kwargs)[:, 0].tolist()
    return fig, axes


def _subtitle(fig: Figure, text: str | None) -> None:
    if text:
        fig.text(0.995, 0.995, text, ha="right", va="top", fontsize=8, color=MUTED)


def _save(fig: Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    return path


def _month_label(month: str) -> str:
    return date.fromisoformat(f"{month}-01").strftime("%b %Y")


def _short_month(month: str) -> str:
    return date.fromisoformat(f"{month}-01").strftime("%b\n%y")


# Charts -------------------------------------------------------------------------------


def monthly_trend_chart(
    totals: Sequence[q.MonthTotal], path: Path, *, note: str | None = None
) -> Path:
    """Line chart of spending (and income) per month with the average spend marked."""
    with matplotlib.rc_context(STYLE):
        fig, (ax,) = _new_figure(10, 4.8)
        x = list(range(len(totals)))
        spent = [t.spent_paise / 100 for t in totals]
        income = [t.income_paise / 100 for t in totals]

        ax.plot(x, spent, color=SERIES_1, lw=2, marker="o", ms=5, label="Spending", zorder=3)
        if any(income):
            ax.plot(x, income, color=SERIES_2, lw=2, marker="o", ms=5, label="Income", zorder=3)

        if spent:
            avg = sum(spent) / len(spent)
            ax.axhline(avg, color=MUTED, lw=1, ls="--", zorder=2)
            ax.annotate(
                f"avg spend {format_compact(avg)}",
                (x[0], avg),
                xytext=(0, 5),
                textcoords="offset points",
                fontsize=8,
                color=INK_2,
            )
            peak = max(range(len(spent)), key=spent.__getitem__)
            ax.annotate(
                format_inr(round(spent[peak] * 100), symbol=RUPEE, decimals=False),
                (x[peak], spent[peak]),
                xytext=(0, 9),
                textcoords="offset points",
                ha="center",
                fontsize=8,
                color=INK,
                fontweight="bold",
            )

        ax.set_xticks(x, [_short_month(t.month) for t in totals])
        ax.yaxis.set_major_formatter(_rupee_axis)
        ax.yaxis.set_major_locator(MaxNLocator(6))
        ax.set_ylim(bottom=0, top=max([*spent, *income, 1]) * 1.15)
        ax.grid(axis="x", visible=False)
        ax.set_ylabel(f"Amount ({RUPEE})")
        span = (
            f"{_month_label(totals[0].month)} - {_month_label(totals[-1].month)}" if totals else ""
        )
        ax.set_title(f"Monthly spending trend  ·  {span}")
        ax.legend(loc="upper left", ncols=2, bbox_to_anchor=(0, 1.02))
        _subtitle(fig, note)
        return _save(fig, path)


def category_breakdown_chart(
    totals: Sequence[q.CategoryTotal],
    month: str,
    path: Path,
    *,
    top_n: int = 10,
    note: str | None = None,
) -> Path:
    """Horizontal bars of spend per category, largest first, small ones folded into Other."""
    with matplotlib.rc_context(STYLE):
        ordered = sorted(totals, key=lambda t: t.spent_paise, reverse=True)
        head, tail = ordered[:top_n], ordered[top_n:]
        labels = [t.category for t in head]
        values = [t.spent_paise / 100 for t in head]
        if tail:
            labels.append(f"Other ({len(tail)})")
            values.append(sum(t.spent_paise for t in tail) / 100)
        total = sum(values)

        fig, (ax,) = _new_figure(10, max(3.2, 0.48 * len(labels) + 1.6))
        if not values:
            ax.text(0.5, 0.5, "No spending this month", ha="center", va="center", color=MUTED)
            ax.set_axis_off()
        else:
            y = list(range(len(values)))[::-1]
            ax.barh(y, values, color=SERIES_1, height=0.62, zorder=3)
            for yi, value in zip(y, values, strict=True):
                share = value * 100 / total if total else 0
                ax.annotate(
                    f"{format_inr(round(value * 100), symbol=RUPEE, decimals=False)}"
                    f"  ·  {share:.0f}%",
                    (value, yi),
                    xytext=(5, 0),
                    textcoords="offset points",
                    va="center",
                    fontsize=9,
                    color=INK_2,
                )
            ax.set_yticks(y, labels)
            ax.tick_params(axis="y", length=0)
            ax.xaxis.set_major_formatter(_rupee_axis)
            ax.set_xlim(0, max(values) * 1.28)
            ax.grid(axis="y", visible=False)
            ax.set_xlabel(f"Spent ({RUPEE})")
        ax.set_title(
            f"Where the money went  ·  {_month_label(month)}  ·  total "
            f"{format_inr(round(total * 100), symbol=RUPEE, decimals=False)}"
        )
        _subtitle(fig, note)
        return _save(fig, path)


def budget_adherence_chart(
    statuses: Sequence[q.BudgetStatus], month: str, path: Path, *, note: str | None = None
) -> Path:
    """Percent of budget used per category, coloured and labelled by status."""
    with matplotlib.rc_context(STYLE):
        rows = sorted(
            (s for s in statuses if s.budget_paise), key=lambda s: s.pct_used or 0, reverse=True
        )
        fig, (ax,) = _new_figure(10, max(3.2, 0.5 * len(rows) + 1.8))
        if not rows:
            ax.text(
                0.5,
                0.5,
                f"No budgets set for {_month_label(month)}\n"
                "(expense budget set CATEGORY AMOUNT --month YYYY-MM)",
                ha="center",
                va="center",
                color=MUTED,
            )
            ax.set_axis_off()
        else:
            y = list(range(len(rows)))[::-1]
            pcts = [s.pct_used or 0.0 for s in rows]
            colors = [STATUS[s.status] for s in rows]
            ax.barh(y, pcts, color=colors, height=0.62, zorder=3)
            ax.axvline(100, color=INK, lw=1.2, zorder=4)
            ax.axvline(q.WARN_THRESHOLD_PCT, color=MUTED, lw=1, ls=":", zorder=4)
            ax.text(100, len(rows) - 0.35, " 100% budget", fontsize=8, color=INK, va="bottom")
            for yi, s, pct in zip(y, rows, pcts, strict=True):
                spent = format_inr(s.spent_paise, symbol=RUPEE, decimals=False)
                budget = format_inr(s.budget_paise or 0, symbol=RUPEE, decimals=False)
                ax.annotate(
                    f"{pct:.0f}%  {spent} of {budget}  {s.status}",
                    (pct, yi),
                    xytext=(5, 0),
                    textcoords="offset points",
                    va="center",
                    fontsize=9,
                    color=INK,
                    fontweight="bold" if s.status == "OVER" else "normal",
                )
            ax.set_yticks(y, [s.category for s in rows])
            ax.tick_params(axis="y", length=0)
            ax.set_xlim(0, max(130.0, max(pcts) * 1.55))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}%"))
            ax.grid(axis="y", visible=False)
            ax.set_xlabel("Budget used (%)")
            over = sum(1 for s in rows if s.status == "OVER")
            fig.text(
                0.01,
                0.005,
                f"OK < {q.WARN_THRESHOLD_PCT:.0f}%  ·  WARN >= {q.WARN_THRESHOLD_PCT:.0f}%"
                f"  ·  OVER > 100%   |   {over} of {len(rows)} categories over budget",
                fontsize=8,
                color=INK_2,
                va="bottom",
            )
        ax.set_title(f"Budget adherence  ·  {_month_label(month)}")
        _subtitle(fig, note)
        return _save(fig, path)


def daily_spending_chart(
    daily: Sequence[tuple[date, int]],
    month: str,
    path: Path,
    *,
    budget_total_paise: int = 0,
    note: str | None = None,
) -> Path:
    """Two aligned panels: spend per day, and cumulative spend against the budget pace."""
    with matplotlib.rc_context(STYLE):
        fig, (top, bottom) = _new_figure(
            10, 6.4, nrows=2, sharex=True, gridspec_kw={"height_ratios": [1.1, 1]}
        )
        days = [d.day for d, _ in daily]
        values = [p / 100 for _, p in daily]
        cumulative, running = [], 0.0
        for v in values:
            running += v
            cumulative.append(running)

        top.bar(days, values, color=SERIES_1, width=0.7, zorder=3)
        if values and max(values) > 0:
            peak = max(range(len(values)), key=values.__getitem__)
            top.annotate(
                format_inr(round(values[peak] * 100), symbol=RUPEE, decimals=False),
                (days[peak], values[peak]),
                xytext=(0, 4),
                textcoords="offset points",
                ha="center",
                fontsize=8,
                color=INK,
                fontweight="bold",
            )
            top.set_ylim(0, max(values) * 1.18)
        top.yaxis.set_major_formatter(_rupee_axis)
        top.yaxis.set_major_locator(MaxNLocator(5))
        top.grid(axis="x", visible=False)
        top.set_ylabel("Spent that day")
        top.set_title(f"Daily spending  ·  {_month_label(month)}")

        bottom.plot(days, cumulative, color=SERIES_1, lw=2, label="Cumulative spend", zorder=3)
        if budget_total_paise and days:
            budget = budget_total_paise / 100
            pace = [budget * d / len(days) for d in days]
            bottom.plot(
                days, pace, color=MUTED, lw=1.5, ls="--", label="Even budget pace", zorder=2
            )
            bottom.axhline(budget, color=INK, lw=1, zorder=2)
            bottom.annotate(
                f"sum of category budgets {format_compact(budget)}",
                (days[0], budget),
                xytext=(0, 4),
                textcoords="offset points",
                fontsize=8,
                color=INK,
            )
        if cumulative:
            bottom.annotate(
                format_inr(round(cumulative[-1] * 100), symbol=RUPEE, decimals=False),
                (days[-1], cumulative[-1]),
                xytext=(-4, 6),
                textcoords="offset points",
                ha="right",
                fontsize=8,
                color=INK,
                fontweight="bold",
            )
        top_value = max([*cumulative, budget_total_paise / 100, 1])
        bottom.set_ylim(0, top_value * 1.15)
        bottom.yaxis.set_major_formatter(_rupee_axis)
        bottom.yaxis.set_major_locator(MaxNLocator(5))
        bottom.set_ylabel("Month to date")
        bottom.set_xlabel("Day of month")
        bottom.set_xticks([d for d in days if d == 1 or d % 5 == 0])
        bottom.set_xlim(0.3, (days[-1] if days else 31) + 0.7)
        bottom.legend(loc="lower right")
        _subtitle(fig, note)
        return _save(fig, path)


# Orchestration ------------------------------------------------------------------------

REPORT_FILES = (
    "monthly_trend.png",
    "category_breakdown.png",
    "budget_adherence.png",
    "daily_spending.png",
)


def generate_report(
    conn: sqlite3.Connection,
    out_dir: str | Path,
    month: str,
    *,
    months: int = 12,
    note: str | None = None,
) -> list[Path]:
    """Query the database and write all four report charts; return their paths."""
    if months < 1:
        raise ValueError("months must be >= 1")
    out = Path(out_dir)
    first = q.add_months(month, -(months - 1))
    overview = q.month_overview(conn, month)
    return [
        monthly_trend_chart(q.monthly_totals(conn, first, month), out / REPORT_FILES[0], note=note),
        category_breakdown_chart(
            q.category_totals(conn, month), month, out / REPORT_FILES[1], note=note
        ),
        budget_adherence_chart(
            q.budget_status(conn, month), month, out / REPORT_FILES[2], note=note
        ),
        daily_spending_chart(
            q.daily_totals(conn, month),
            month,
            out / REPORT_FILES[3],
            budget_total_paise=overview.budget_total_paise,
            note=note,
        ),
    ]
