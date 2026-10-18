"""Rule-based auto-categorisation.

Rules live in the ``rules`` table so users can edit them without touching code.
They are loaded and compiled once per command (not once per row), then applied to
each normalised description. The first matching rule in (priority DESC, id ASC)
order wins.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from expense_dashboard.db import transaction


class RuleError(ValueError):
    """Raised for an invalid rule (bad regex, unknown category, empty pattern)."""


@dataclass(frozen=True)
class Rule:
    id: int
    pattern: str
    match_type: str
    category_id: int
    category: str
    priority: int
    regex: re.Pattern[str]

    def matches(self, description: str) -> bool:
        return self.regex.search(description) is not None


def compile_pattern(pattern: str, match_type: str) -> re.Pattern[str]:
    """Compile a rule pattern.

    ``keyword`` rules match whole tokens only: the keyword must not be glued to a
    letter or digit on either side, so ``ola`` matches ``UPI/OLA CABS`` but not
    ``GRANOLA``. Internal whitespace in a keyword matches any run of whitespace.
    """
    pattern = pattern.strip()
    if not pattern:
        raise RuleError("rule pattern must not be empty")
    if match_type == "keyword":
        body = r"\s+".join(re.escape(part) for part in pattern.split())
        return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.IGNORECASE)
    if match_type == "regex":
        try:
            return re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise RuleError(f"invalid regular expression {pattern!r}: {exc}") from exc
    raise RuleError(f"match_type must be 'keyword' or 'regex', got {match_type!r}")


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    rows = conn.execute(
        """
        SELECT r.id, r.pattern, r.match_type, r.category_id, c.name AS category, r.priority
        FROM rules r JOIN categories c ON c.id = r.category_id
        ORDER BY r.priority DESC, r.id ASC
        """
    ).fetchall()
    return [
        Rule(
            id=row["id"],
            pattern=row["pattern"],
            match_type=row["match_type"],
            category_id=row["category_id"],
            category=row["category"],
            priority=row["priority"],
            regex=compile_pattern(row["pattern"], row["match_type"]),
        )
        for row in rows
    ]


def match(rules: list[Rule], description: str) -> Rule | None:
    """Return the winning rule for a description, or ``None``. ``rules`` must be sorted."""
    for rule in rules:
        if rule.matches(description):
            return rule
    return None


def category_id_by_name(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name.strip(),)).fetchone()
    if row is None:
        raise RuleError(f"unknown category {name!r} (see `expense categories list`)")
    return int(row["id"])


def add_rule(
    conn: sqlite3.Connection,
    pattern: str,
    category: str,
    *,
    match_type: str = "keyword",
    priority: int = 0,
) -> int:
    compile_pattern(pattern, match_type)  # validate before it reaches the database
    category_id = category_id_by_name(conn, category)
    try:
        cur = conn.execute(
            "INSERT INTO rules (pattern, match_type, category_id, priority) VALUES (?, ?, ?, ?)",
            (pattern.strip(), match_type, category_id, priority),
        )
    except sqlite3.IntegrityError as exc:
        raise RuleError(f"a {match_type} rule for {pattern!r} already exists") from exc
    return int(cur.lastrowid)


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    return conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount == 1


@dataclass(frozen=True)
class CategorizeResult:
    examined: int
    changed: int
    uncategorized: int


def apply_rules(
    conn: sqlite3.Connection, *, include_rule_assigned: bool = False, dry_run: bool = False
) -> CategorizeResult:
    """Re-run the rules over stored transactions.

    By default only uncategorised rows are touched. With ``include_rule_assigned``
    rows previously categorised by a rule are re-evaluated too (useful after editing
    rules). Rows the user categorised manually are never overwritten.
    """
    rules = load_rules(conn)
    where = "categorized_by IS NULL"
    if include_rule_assigned:
        where += " OR categorized_by = 'rule'"
    rows = conn.execute(
        f"SELECT id, description, category_id, rule_id FROM transactions WHERE {where}"
    ).fetchall()

    updates: list[tuple[int | None, int | None, str | None, int]] = []
    still_uncategorized = 0
    for row in rows:
        rule = match(rules, row["description"])
        new = (rule.category_id, rule.id) if rule else (None, None)
        if rule is None:
            still_uncategorized += 1
        if new != (row["category_id"], row["rule_id"]):
            updates.append((new[0], new[1], "rule" if rule else None, row["id"]))

    if updates and not dry_run:
        with transaction(conn):
            conn.executemany(
                "UPDATE transactions SET category_id = ?, rule_id = ?, categorized_by = ? "
                "WHERE id = ?",
                updates,
            )
    return CategorizeResult(
        examined=len(rows), changed=len(updates), uncategorized=still_uncategorized
    )


def set_category(conn: sqlite3.Connection, txn_id: int, category: str | None) -> None:
    """Manually (re)categorise one transaction; ``None`` clears it back to uncategorised."""
    if category is None:
        values: tuple[int | None, str | None] = (None, None)
    else:
        values = (category_id_by_name(conn, category), "manual")
    cur = conn.execute(
        "UPDATE transactions SET category_id = ?, categorized_by = ?, rule_id = NULL WHERE id = ?",
        (*values, txn_id),
    )
    if cur.rowcount != 1:
        raise RuleError(f"no transaction with id {txn_id}")
