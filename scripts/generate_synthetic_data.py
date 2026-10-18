"""Write SYNTHETIC bank and credit-card statement CSVs without touching a database.

    python scripts/generate_synthetic_data.py --out demo_data --months 12 --seed 42

Useful for trying the import pipeline by hand:

    expense init
    expense import demo_data/savings_account.csv --skip-rows 2
    expense import demo_data/credit_card.csv --debit-positive
"""

from __future__ import annotations

import argparse
from datetime import date

from expense_dashboard import queries as q
from expense_dashboard.demo import generate_transactions, write_statements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="demo_data")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--end-month", default=q.add_months(date.today().strftime("%Y-%m"), -1))
    args = parser.parse_args()

    txns = generate_transactions(args.seed, q.parse_month(args.end_month), args.months)
    bank, card = write_statements(txns, args.out)
    print(f"wrote {len(txns)} synthetic transactions to {bank} and {card}")


if __name__ == "__main__":
    main()
