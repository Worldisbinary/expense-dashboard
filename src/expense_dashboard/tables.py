"""Dependency-free terminal tables.

Uses plain ASCII rules so output survives any console code page and copy-pastes
cleanly into Markdown. Column widths are measured on the rendered strings.
"""

from __future__ import annotations

from collections.abc import Sequence


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    align: str | None = None,
    max_width: int = 48,
    footer: Sequence[object] | None = None,
) -> str:
    """Render rows as an aligned text table.

    ``align`` is one character per column: ``l`` (left) or ``r`` (right). Cells
    longer than ``max_width`` are truncated with an ellipsis. An optional ``footer``
    row (e.g. totals) is separated by a rule.
    """
    ncols = len(headers)
    align = (align or "l" * ncols).ljust(ncols, "l")

    def cell(value: object) -> str:
        text = "" if value is None else str(value)
        return text if len(text) <= max_width else text[: max_width - 3] + "..."

    body = [[cell(v) for v in row] for row in rows]
    foot = [cell(v) for v in footer] if footer is not None else None
    for row in [*body, *([foot] if foot else [])]:
        if len(row) != ncols:
            raise ValueError(f"row has {len(row)} cells, expected {ncols}")

    widths = [len(h) for h in headers]
    for row in [*body, *([foot] if foot else [])]:
        widths = [max(w, len(c)) for w, c in zip(widths, row, strict=True)]

    def line(cells: Sequence[str]) -> str:
        parts = [
            c.rjust(w) if a == "r" else c.ljust(w)
            for c, w, a in zip(cells, widths, align, strict=True)
        ]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * w for w in widths)
    out = [line(list(headers)), rule, *(line(r) for r in body)]
    if foot:
        out += [rule, line(foot)]
    return "\n".join(out)
