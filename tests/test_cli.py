"""CLI smoke and behaviour tests through ``main(argv)``, plus real subprocess runs."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from expense_dashboard import __version__
from expense_dashboard.cli import main
from tests.test_etl import BANK_CSV


@pytest.fixture
def cli(db_path, capsys):
    """Run ``expense --db <tmp> ...`` in-process and return (exit_code, stdout, stderr)."""

    def run(*args: str):
        code = main(["--db", str(db_path), *args])
        out, err = capsys.readouterr()
        return code, out, err

    return run


@pytest.fixture
def ready(cli):
    assert cli("init")[0] == 0
    return cli


def test_init_creates_schema_and_defaults(cli):
    code, out, _ = cli("init")
    assert code == 0
    assert "applied migration 0001_initial.sql" in out
    assert "default categories" in out
    code, out, _ = cli("init")
    assert "up to date" in out


def test_init_no_defaults(cli):
    cli("init", "--no-defaults")
    code, out, _ = cli("categories", "list")
    assert code == 0 and "Groceries" not in out


def test_commands_require_init(cli):
    code, _, err = cli("summary")
    assert code == 1 and "expense init" in err


def test_add_list_delete(ready):
    code, out, _ = ready("add", "249", "UPI/ZOMATO/1", "--date", "2026-03-04")
    assert code == 0 and "[Food Delivery]" in out and "₹249.00" in out
    code, out, _ = ready(
        "add",
        "1,500.50",
        "Birthday gift",
        "--date",
        "2026-03-05",
        "--category",
        "Shopping",
        "--note",
        "for Riya",
    )
    assert code == 0
    code, out, _ = ready("add", "5000", "Freelance", "--income", "--date", "2026-03-06")
    assert code == 0
    code, out, _ = ready("list", "--month", "2026-03")
    assert "UPI/ZOMATO/1" in out and "Shopping *" in out and "3 transaction(s)" in out
    assert "₹3,250.50" in out  # net: 5000 - 249 - 1500.50
    code, out, _ = ready("delete", "1")
    assert code == 0
    code, _, err = ready("delete", "1")
    assert code == 1 and "no transaction" in err


def test_add_validation_errors(ready):
    assert ready("add", "0", "x")[0] == 1
    code, _, err = ready("add", "10", "x", "--date", "04/03/2026")
    assert code == 1 and "YYYY-MM-DD" in err
    code, _, err = ready("add", "10", "x", "--category", "Nope")
    assert code == 1 and "unknown category" in err


def test_import_reimport_dry_run_and_batches(ready, write_csv):
    path = str(write_csv("bank.csv", BANK_CSV))
    code, out, _ = ready("import", path, "--dry-run")
    assert "would insert 4" in out
    code, out, _ = ready("import", path)
    assert code == 0 and "inserted 4" in out and "(batch #1)" in out
    code, out, _ = ready("import", path)
    assert "inserted 0, skipped 4 duplicate(s)" in out
    code, out, _ = ready("batches", "list")
    assert "bank.csv" in out and out.count("bank.csv") == 2
    code, out, _ = ready("batches", "undo", "1")
    assert "4 transaction(s)" in out
    assert "no matching" in ready("list")[1]


def test_import_flags_and_mapping_file(ready, write_csv, tmp_path):
    card = write_csv("card.csv", "When;What;Value\n2026-03-07;ZEPTO;₹1,299.00\n")
    code, out, _ = ready(
        "import",
        str(card),
        "--date-col",
        "When",
        "--desc-col",
        "What",
        "--amount-col",
        "Value",
        "--delimiter",
        ";",
        "--debit-positive",
    )
    assert code == 0 and "inserted 1" in out
    mapping = tmp_path / "m.json"
    mapping.write_text(
        '{"date": "When", "description": "What", "amount": "Value", '
        '"delimiter": ";", "amount_sign": "debit_positive"}'
    )
    card2 = write_csv("card2.csv", "When;What;Value\n2026-03-08;BLINKIT;₹99\n")
    code, out, _ = ready("import", str(card2), "--mapping", str(mapping))
    assert code == 0 and "inserted 1" in out
    code, _, err = ready("import", str(card2), "--mapping", str(mapping), "--delimiter", ";")
    assert code == 1 and "not both" in err


def test_import_rejects_and_strict(ready, write_csv):
    bad = str(write_csv("bad.csv", BANK_CSV + "99/99/2026,BROKEN,1,\n"))
    code, out, _ = ready("import", bad, "--dry-run")
    assert code == 0 and "rejected 1" in out and "line 6" in out
    code, _, _ = ready("import", bad, "--dry-run", "--fail-on-reject")
    assert code == 1
    code, _, err = ready("import", bad, "--strict")
    assert code == 1 and "invalid row" in err
    code, _, err = ready("import", "does-not-exist.csv")
    assert code == 1 and "file not found" in err


def test_import_reports_many_rejects_compactly(ready, write_csv):
    rows = "".join(f"bad{i},X,1,\n" for i in range(12))
    path = write_csv("many.csv", "Date,Narration,Debit,Credit\n" + rows)
    _code, out, _ = ready("import", str(path))
    assert "and 2 more rejected" in out


def test_categories_and_rules_commands(ready):
    assert ready("categories", "add", "Pets")[0] == 0
    code, out, _ = ready("categories", "list")
    assert "Pets" in out and "transfer" in out
    code, out, _ = ready("rules", "add", "heads up", "Pets", "--priority", "200")
    assert code == 0 and "added rule" in out
    code, out, _ = ready("rules", "add", r"\bvet(erinary)?\b", "Pets", "--regex")
    assert code == 0
    code, out, _ = ready("rules", "test", "HEADS   UP FOR TAILS")
    assert "-> Pets" in out
    code, out, _ = ready("rules", "test", "nothing matches this")
    assert "Uncategorized" in out
    code, out, _ = ready("rules", "list")
    assert "heads up" in out and "regex" in out
    rule_id = out.splitlines()[2].split()[0]  # highest-priority rule is listed first
    assert ready("rules", "delete", rule_id)[0] == 0
    code, _, err = ready("rules", "delete", rule_id)
    assert code == 1 and "no rule" in err
    code, _, err = ready("rules", "add", "(", "Pets", "--regex")
    assert code == 1 and "invalid regular expression" in err
    assert ready("categories", "delete", "Pets")[0] == 0
    code, _, err = ready("categories", "add", "groceries")
    assert code == 1 and "already exists" in err


def test_categorize_command(ready):
    ready("add", "30", "UPI/RAJU TEA STALL/1", "--date", "2026-03-01")
    ready("rules", "add", "tea stall", "Dining Out")
    code, out, _ = ready("categorize", "--dry-run")
    assert "would change 1" in out
    code, out, _ = ready("categorize")
    assert "changed 1" in out and "0 still uncategorised" in out
    code, out, _ = ready("categorize", "--txn", "1", "--category", "Groceries")
    assert "(manual)" in out
    code, out, _ = ready("categorize", "--all")
    assert "changed 0" in out  # manual assignment survives
    code, out, _ = ready("categorize", "--txn", "1", "--clear")
    assert "uncategorised" in out
    code, _, err = ready("categorize", "--txn", "1")
    assert code == 1 and "--category" in err


def test_budget_commands(ready):
    code, out, _ = ready(
        "budget", "set", "Groceries", "8000", "--month", "2026-01", "--through", "2026-03"
    )
    assert code == 0 and "2026-01..2026-03" in out
    code, out, _ = ready("budget", "set", "Fuel", "3000", "--month", "2026-02")
    assert "(2026-02)" in out
    code, out, _ = ready("budget", "list", "--month", "2026-02")
    assert "Groceries" in out and "Fuel" in out and "₹8,000.00" in out
    assert ready("budget", "delete", "Fuel", "--month", "2026-02")[0] == 0
    code, _, err = ready("budget", "delete", "Fuel", "--month", "2026-02")
    assert code == 1 and "no budget" in err
    code, _, err = ready(
        "budget", "set", "Fuel", "100", "--month", "2026-05", "--through", "2026-04"
    )
    assert code == 1 and "--through" in err
    code, _, err = ready("budget", "set", "Investments", "100", "--month", "2026-05")
    assert code == 1 and "expense categories" in err
    assert ready("budget", "set", "Health", "500")[0] == 0  # defaults to current month


def test_budget_list_empty(ready):
    assert "no budgets" in ready("budget", "list")[1]


def test_summary_output(ready):
    ready("add", "1000", "Veg", "--date", "2026-01-05", "--category", "Groceries")
    ready("add", "250.50", "Dinner", "--date", "2026-01-20", "--category", "Food Delivery")
    ready("add", "99", "Mystery", "--date", "2026-01-21")
    ready("add", "800", "Veg", "--date", "2025-12-05", "--category", "Groceries")
    ready("budget", "set", "Groceries", "1200", "--month", "2026-01")
    ready("budget", "set", "Food Delivery", "200", "--month", "2026-01")
    code, out, _ = ready("summary")  # defaults to the latest month with data
    assert code == 0
    assert "Summary for Jan 2026" in out
    assert "₹1,349.50" in out  # total spent
    assert "+68.7% vs Dec 2025" in out  # (1349.50 - 800) / 800
    assert "125%" in out and "OVER" in out and "83%" in out and "WARN" in out
    assert "Uncategorized" in out
    assert "OVER BUDGET in 1 category: Food Delivery (+₹50.50)" in out
    code, out, _ = ready("summary", "--month", "2025-12")
    assert "Summary for Dec 2025" in out and "OVER BUDGET" not in out


def test_summary_without_data(ready):
    code, _, err = ready("summary")
    assert code == 1 and "no transactions yet" in err
    code, out, _ = ready("summary", "--month", "2026-01")
    assert code == 0 and "no spending or budgets" in out


def test_report_command_writes_pngs(ready, tmp_path):
    ready("add", "1000", "Veg", "--date", "2026-01-05", "--category", "Groceries")
    out_dir = tmp_path / "charts"
    code, out, _ = ready("report", "--out", str(out_dir), "--months", "3", "--note", "unit test")
    assert code == 0 and "wrote 4 chart(s) for Jan 2026" in out
    files = sorted(p.name for p in out_dir.iterdir())
    assert files == sorted(
        [
            "monthly_trend.png",
            "category_breakdown.png",
            "budget_adherence.png",
            "daily_spending.png",
        ]
    )
    assert all((out_dir / f).read_bytes().startswith(b"\x89PNG") for f in files)


def test_explain_command(ready):
    code, out, _ = ready("explain")
    assert code == 0
    assert out.count("USED") == 6 and "NOT USED" not in out
    assert "SEARCH t USING INDEX idx_transactions_category_date" in out
    code, out, _ = ready("explain", "--query", "dedupe_probe", "--benchmark", "--repeat", "2")
    assert code == 0 and "Speed-up" in out and "monthly_trend" not in out
    code, _, err = ready("explain", "--query", "nope")
    assert code == 1 and "unknown query" in err


def test_explain_fails_when_index_missing(ready, db_path):
    import sqlite3

    raw = sqlite3.connect(db_path)
    raw.execute("DROP INDEX idx_transactions_date")
    raw.commit()
    raw.close()
    code, out, _ = ready("explain")
    assert code == 1 and "NOT USED" in out


def test_demo_command(cli, db_path, tmp_path):
    data, charts_dir = tmp_path / "data", tmp_path / "charts"
    code, out, _ = cli(
        "demo",
        "--end-month",
        "2026-06",
        "--months",
        "3",
        "--data-dir",
        str(data),
        "--report",
        str(charts_dir),
    )
    assert code == 0 and "SYNTHETIC" in out and out.count("chart:") == 4
    assert (data / "savings_account.csv").exists()
    code, _, err = cli("demo", "--data-dir", str(data))
    assert code == 1 and "--force" in err
    code, out, _ = cli("demo", "--force", "--months", "1", "--data-dir", str(data))
    assert code == 0
    code, out, _ = cli("summary")
    assert code == 0 and "Summary for" in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_integrity_error_is_reported(ready, monkeypatch):
    import sqlite3

    from expense_dashboard import queries

    def boom(*_a, **_k):
        raise sqlite3.IntegrityError("CHECK constraint failed: demo")

    monkeypatch.setattr(queries, "add_transaction", boom)
    code, _, err = ready("add", "10", "x")
    assert code == 1 and "database constraint" in err


# ------------------------------------------------------------------ real processes


def _run(*args, cwd, env_extra=None):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "expense_dashboard", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def test_subprocess_end_to_end(tmp_path, write_csv):
    csv_path = write_csv("bank.csv", BANK_CSV)
    db_file = tmp_path / "e2e.db"
    env = {"EXPENSE_DB": str(db_file)}  # --db falls back to $EXPENSE_DB
    assert _run("init", cwd=tmp_path, env_extra=env).returncode == 0
    result = _run("import", str(csv_path), cwd=tmp_path, env_extra=env)
    assert result.returncode == 0, result.stderr
    assert "inserted 4" in result.stdout
    result = _run("summary", cwd=tmp_path, env_extra=env)
    assert result.returncode == 0 and "Summary for Mar 2026" in result.stdout
    assert db_file.exists()


def test_subprocess_bad_arguments_exit_2(tmp_path):
    result = _run("budget", cwd=tmp_path)
    assert result.returncode == 2 and "usage:" in result.stderr


def test_subprocess_rupee_falls_back_on_narrow_console(tmp_path, write_csv):
    db_file = tmp_path / "x.db"
    _run("--db", str(db_file), "init", cwd=tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "expense_dashboard",
            "--db",
            str(db_file),
            "add",
            "5",
            "tea",
            "--date",
            "2026-01-01",
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
    )
    assert result.returncode == 0
    assert b"Rs.5.00" in result.stdout
