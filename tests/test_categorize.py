from __future__ import annotations

from datetime import date

import pytest

from expense_dashboard import categorize as cat
from expense_dashboard import queries as q


@pytest.mark.parametrize(
    ("description", "matches"),
    [
        ("UPI/OLA CABS/1234", True),
        ("ola", True),
        ("Ola-ride", True),
        ("GRANOLA BAR", False),  # whole tokens only
        ("OLAF STORE", False),
        ("COLA", False),
        ("ola2", False),
    ],
)
def test_keyword_rules_match_whole_tokens(description, matches):
    assert (cat.compile_pattern("ola", "keyword").search(description) is not None) is matches


def test_keyword_with_spaces_matches_any_whitespace_and_escapes_metacharacters():
    rx = cat.compile_pattern("youtube  premium", "keyword")
    assert rx.search("GOOGLE YOUTUBE    PREMIUM")
    assert not rx.search("YOUTUBEPREMIUM")
    assert cat.compile_pattern("1mg.com", "keyword").search("PAY 1MG.COM")
    assert not cat.compile_pattern("1mg.com", "keyword").search("PAY 1MGXCOM")  # '.' is literal


def test_regex_rules_are_case_insensitive():
    rx = cat.compile_pattern(r"\b(hpcl|iocl)\b", "regex")
    assert rx.search("pos/Hpcl fuel")


@pytest.mark.parametrize(
    ("pattern", "match_type", "message"),
    [
        ("  ", "keyword", "empty"),
        ("([unclosed", "regex", "invalid regular"),
        ("x", "fuzzy", "match_type"),
    ],
)
def test_invalid_patterns(pattern, match_type, message):
    with pytest.raises(cat.RuleError, match=message):
        cat.compile_pattern(pattern, match_type)


def test_priority_order_and_tie_break(bare_conn):
    for name in ("Groceries", "Food Delivery", "Shopping"):
        q.add_category(bare_conn, name)
    cat.add_rule(bare_conn, "swiggy", "Food Delivery", priority=50)
    cat.add_rule(bare_conn, "instamart", "Groceries", priority=55)
    assert cat.match(cat.load_rules(bare_conn), "UPI/SWIGGY INSTAMART/1").category == "Groceries"
    assert cat.match(cat.load_rules(bare_conn), "UPI/SWIGGY/1").category == "Food Delivery"
    # equal priority: the older rule (lower id) wins
    cat.add_rule(bare_conn, "store", "Shopping", priority=10)
    cat.add_rule(bare_conn, "general store", "Groceries", priority=10)
    assert cat.match(cat.load_rules(bare_conn), "GENERAL STORE").category == "Shopping"
    assert cat.match(cat.load_rules(bare_conn), "NOTHING HERE") is None


def test_default_rules_resolve_common_narrations(conn):
    rules = cat.load_rules(conn)
    cases = {
        "NEFT/ACME LTD/SALARY SEP 2026": "Income",
        "UPI/SWIGGY/412345678901": "Food Delivery",
        "UPI/SWIGGY INSTAMART/412345678901": "Groceries",
        "AMAZON PRIME MEMBERSHIP": "Subscriptions",
        "AMAZON PAY INDIA": "Shopping",
        "POS/HPCL FUEL STATION/1": "Fuel",
        "BBPS/BESCOM ELECTRICITY BILL/1": "Utilities",
        "BILLDESK/CREDIT CARD PAYMENT/1": "Credit Card Payment",
        "NACH/ZERODHA BROKING LTD/SIP": "Investments",
        "UPI/RAMESH KUMAR/1/HOUSE RENT": "Rent",
        "ATM CASH WDL/MG ROAD/1": "Cash Withdrawal",
        "GRANOLA HOUSE CAFE": "Dining Out",  # 'cafe' rule, and 'ola' must NOT fire
    }
    for description, expected in cases.items():
        hit = cat.match(rules, description)
        assert hit is not None and hit.category == expected, description
    assert cat.match(rules, "UPI/RAJU TEA STALL/1") is None


def test_add_rule_validation(conn):
    with pytest.raises(cat.RuleError, match="unknown category"):
        cat.add_rule(conn, "foo", "No Such Category")
    with pytest.raises(cat.RuleError, match="invalid regular"):
        cat.add_rule(conn, "(", "Groceries", match_type="regex")
    with pytest.raises(cat.RuleError, match="already exists"):
        cat.add_rule(conn, "swiggy", "Groceries")  # seeded by defaults
    assert conn.execute("SELECT COUNT(*) FROM rules WHERE pattern = '('").fetchone()[0] == 0


def test_delete_rule(conn):
    rule_id = cat.add_rule(conn, "tea stall", "Dining Out")
    assert cat.delete_rule(conn, rule_id)
    assert not cat.delete_rule(conn, rule_id)


def _add(conn, desc, category=None):
    return q.add_transaction(
        conn, txn_date=date(2026, 3, 1), description=desc, amount_paise=100, category=category
    )


def _category_of(conn, txn_id):
    row = conn.execute(
        "SELECT c.name, t.categorized_by FROM transactions t "
        "LEFT JOIN categories c ON c.id = t.category_id WHERE t.id = ?",
        (txn_id,),
    ).fetchone()
    return tuple(row)


def test_apply_rules_categorises_new_matches_but_respects_manual(conn):
    tea = _add(conn, "UPI/RAJU TEA STALL/1")
    manual = _add(conn, "UPI/RAJU TEA STALL/2", category="Groceries")
    assert _category_of(conn, tea) == (None, None)

    cat.add_rule(conn, "tea stall", "Dining Out")
    preview = cat.apply_rules(conn, dry_run=True)
    assert (preview.examined, preview.changed) == (1, 1)
    assert _category_of(conn, tea) == (None, None)  # dry run wrote nothing

    result = cat.apply_rules(conn)
    assert (result.examined, result.changed, result.uncategorized) == (1, 1, 0)
    assert _category_of(conn, tea) == ("Dining Out", "rule")
    assert _category_of(conn, manual) == ("Groceries", "manual")


def test_apply_rules_all_reevaluates_rule_assigned_rows(conn):
    txn = _add(conn, "UPI/SWIGGY/1")
    assert _category_of(conn, txn) == ("Food Delivery", "rule")
    cat.add_rule(conn, "swiggy", "Dining Out", match_type="regex", priority=99)
    assert cat.apply_rules(conn).changed == 0  # only uncategorised rows by default
    assert cat.apply_rules(conn, include_rule_assigned=True).changed == 1
    assert _category_of(conn, txn) == ("Dining Out", "rule")


def test_deleting_a_rule_then_reapplying_uncategorises(conn):
    txn = _add(conn, "SOMETHING ODD")
    rule_id = cat.add_rule(conn, "odd", "Shopping")
    cat.apply_rules(conn)
    cat.delete_rule(conn, rule_id)
    result = cat.apply_rules(conn, include_rule_assigned=True)
    assert result.uncategorized == 1
    assert _category_of(conn, txn) == (None, None)


def test_set_category_manual_and_clear(conn):
    txn = _add(conn, "UPI/SWIGGY/1")
    cat.set_category(conn, txn, "Dining Out")
    assert _category_of(conn, txn) == ("Dining Out", "manual")
    cat.set_category(conn, txn, None)
    assert _category_of(conn, txn) == (None, None)
    with pytest.raises(cat.RuleError, match="no transaction"):
        cat.set_category(conn, 9999, "Groceries")
