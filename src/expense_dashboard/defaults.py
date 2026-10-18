"""Starter categories and categorisation rules created by ``expense init``.

They are ordinary rows: users can list, add and delete them with ``expense
categories`` and ``expense rules``. The keywords target common Indian merchants and
statement narrations (UPI, NEFT, POS, ACH).
"""

from __future__ import annotations

import sqlite3

from expense_dashboard.db import transaction

# (name, kind)
DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("Rent", "expense"),
    ("Groceries", "expense"),
    ("Food Delivery", "expense"),
    ("Dining Out", "expense"),
    ("Fuel", "expense"),
    ("Transport", "expense"),
    ("Utilities", "expense"),
    ("Mobile & Internet", "expense"),
    ("Subscriptions", "expense"),
    ("Shopping", "expense"),
    ("Health", "expense"),
    ("Entertainment", "expense"),
    ("Travel", "expense"),
    ("Personal Care", "expense"),
    ("Cash Withdrawal", "expense"),
    ("Income", "income"),
    ("Investments", "transfer"),
    ("Credit Card Payment", "transfer"),
]

# (pattern, match_type, category, priority)
DEFAULT_RULES: list[tuple[str, str, str, int]] = [
    # Income / transfers first: high priority so 'SALARY' beats any merchant keyword
    ("salary", "keyword", "Income", 100),
    (r"\binterest\s+(credit|paid)\b", "regex", "Income", 90),
    ("zerodha", "keyword", "Investments", 80),
    ("groww", "keyword", "Investments", 80),
    (r"\b(sip|mutual\s*fund|nach.*mf)\b", "regex", "Investments", 70),
    (r"\b(cc|credit\s*card)\s*(bill|payment)\b", "regex", "Credit Card Payment", 80),
    ("payment received", "keyword", "Credit Card Payment", 80),  # card-side of the bill
    ("rent", "keyword", "Rent", 60),
    ("nobroker", "keyword", "Rent", 60),
    # Food
    ("swiggy", "keyword", "Food Delivery", 50),
    ("zomato", "keyword", "Food Delivery", 50),
    ("instamart", "keyword", "Groceries", 55),  # Swiggy Instamart is groceries
    ("blinkit", "keyword", "Groceries", 50),
    ("zepto", "keyword", "Groceries", 50),
    ("bigbasket", "keyword", "Groceries", 50),
    ("dmart", "keyword", "Groceries", 50),
    (r"\b(kirana|supermarket|general\s+store)\b", "regex", "Groceries", 40),
    (r"\b(restaurant|cafe|chaayos|starbucks|haldiram|barbeque)\b", "regex", "Dining Out", 40),
    # Getting around
    (r"\b(hpcl|iocl|bpcl|indian\s*oil|petrol|fuel)\b", "regex", "Fuel", 50),
    ("uber", "keyword", "Transport", 50),
    ("ola", "keyword", "Transport", 50),
    ("rapido", "keyword", "Transport", 50),
    (r"\bmetro\b", "regex", "Transport", 45),
    ("fastag", "keyword", "Transport", 45),
    ("irctc", "keyword", "Travel", 50),
    ("makemytrip", "keyword", "Travel", 50),
    ("indigo", "keyword", "Travel", 50),
    # Bills
    (r"\b(electricity|bescom|tata\s*power|adani\s*electricity|msedcl)\b", "regex", "Utilities", 50),
    (r"\b(water\s*bill|piped\s*gas|igl|mgl)\b", "regex", "Utilities", 45),
    (r"\b(jio|airtel|vi\s*prepaid|act\s*fibernet|broadband)\b", "regex", "Mobile & Internet", 50),
    # Subscriptions
    ("netflix", "keyword", "Subscriptions", 50),
    ("spotify", "keyword", "Subscriptions", 50),
    ("hotstar", "keyword", "Subscriptions", 50),
    ("youtube premium", "keyword", "Subscriptions", 55),
    ("prime video", "keyword", "Subscriptions", 55),
    ("amazon prime", "keyword", "Subscriptions", 55),
    # Shopping / health / fun
    ("amazon", "keyword", "Shopping", 30),
    ("flipkart", "keyword", "Shopping", 30),
    ("myntra", "keyword", "Shopping", 30),
    ("ajio", "keyword", "Shopping", 30),
    (r"\b(apollo|pharmeasy|1mg|medplus|pharmacy|hospital|clinic)\b", "regex", "Health", 50),
    (r"\b(pvr|inox|bookmyshow)\b", "regex", "Entertainment", 50),
    (r"\b(salon|urban\s*company|nykaa)\b", "regex", "Personal Care", 45),
    (r"\b(atm|cash\s*wdl|nwd)\b", "regex", "Cash Withdrawal", 60),
]


def seed_defaults(conn: sqlite3.Connection) -> tuple[int, int]:
    """Insert the starter categories and rules; rows that already exist are left alone.

    Returns ``(categories_added, rules_added)``.
    """
    with transaction(conn):
        cats = conn.executemany(
            "INSERT OR IGNORE INTO categories (name, kind) VALUES (?, ?)", DEFAULT_CATEGORIES
        ).rowcount
        rules = conn.executemany(
            """
            INSERT OR IGNORE INTO rules (pattern, match_type, category_id, priority)
            SELECT ?, ?, id, ? FROM categories WHERE name = ?
            """,
            [(pattern, kind, priority, cat) for pattern, kind, cat, priority in DEFAULT_RULES],
        ).rowcount
    return cats, rules
