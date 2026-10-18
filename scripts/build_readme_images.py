"""Regenerate the README screenshots in docs/images from SYNTHETIC demo data.

    python scripts/build_readme_images.py

Uses a fixed seed and end month, so the charts are reproducible byte-for-byte on
the same matplotlib version. The demo database is built in a temporary directory
and thrown away afterwards.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from expense_dashboard import db
from expense_dashboard.charts import generate_report
from expense_dashboard.defaults import seed_defaults
from expense_dashboard.demo import load_demo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
SEED = 42
END_MONTH = "2026-09"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "demo.db")
        try:
            db.migrate(conn)
            seed_defaults(conn)
            load_demo(conn, Path(tmp) / "data", seed=SEED, end_month=END_MONTH, months=12)
            paths = generate_report(
                conn, OUT, END_MONTH, months=12, note="Synthetic demo data (seed 42)"
            )
        finally:
            conn.close()
    for path in paths:
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
