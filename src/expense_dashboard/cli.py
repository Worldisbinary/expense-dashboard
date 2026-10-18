"""``expense``: the command-line entry point (argparse, stdlib only)."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from expense_dashboard import __version__, categorize, db, etl
from expense_dashboard import queries as q
from expense_dashboard.defaults import seed_defaults
from expense_dashboard.money import AmountError, format_inr, rupees_to_paise
from expense_dashboard.tables import render_table

DEFAULT_DB = "expenses.db"


class CliError(Exception):
    """A user-facing error: printed as ``error: ...`` with exit status 1."""


def _out(text: str = "") -> None:
    print(text)


def _money(paise: int | None, decimals: bool = True) -> str:
    return "" if paise is None else format_inr(paise, decimals=decimals)


def _month_name(month: str) -> str:
    return date.fromisoformat(f"{month}-01").strftime("%b %Y")


def _connect(args: argparse.Namespace, path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection that main() closes when the command finishes."""
    conn = db.connect(path or args.db)
    args.connections.append(conn)
    return conn


def _open(args: argparse.Namespace) -> sqlite3.Connection:
    conn = _connect(args)
    db.require_schema(conn)
    return conn


def _resolve_month(conn: sqlite3.Connection, month: str | None) -> str:
    if month:
        return q.parse_month(month)
    latest = q.latest_month(conn)
    if latest is None:
        raise CliError("no transactions yet; `expense import` a statement or `expense add` one")
    return latest


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    conn = _connect(args)
    fresh = db.schema_version(conn) == 0
    applied = db.migrate(conn)
    for name in applied:
        _out(f"applied migration {name}")
    if fresh and not args.no_defaults:
        cats, rules = seed_defaults(conn)
        _out(f"added {cats} default categories and {rules} categorisation rules")
    if not applied:
        _out(f"database is up to date (schema version {db.schema_version(conn)})")
    _out(f"database: {Path(args.db).resolve()}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        txn_date = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError as exc:
        raise CliError(f"--date must be YYYY-MM-DD: {exc}") from exc
    txn_id = q.add_transaction(
        conn,
        txn_date=txn_date,
        description=args.description,
        amount_paise=rupees_to_paise(args.amount),
        direction="credit" if args.income else "debit",
        category=args.category,
        notes=args.note,
    )
    row = conn.execute(
        "SELECT t.id, t.txn_date, t.description, t.amount_paise, c.name AS category "
        "FROM transactions t LEFT JOIN categories c ON c.id = t.category_id WHERE t.id = ?",
        (txn_id,),
    ).fetchone()
    _out(
        f"added #{row['id']}  {row['txn_date']}  {_money(row['amount_paise'])}  "
        f"{row['description']}  [{row['category'] or 'Uncategorized'}]"
    )
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    conn = _open(args)
    if not q.delete_transaction(conn, args.id):
        raise CliError(f"no transaction with id {args.id}")
    _out(f"deleted transaction #{args.id}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    conn = _open(args)
    mapping = etl.ColumnMapping.from_json_file(args.mapping) if args.mapping else None
    overrides = {
        "date": args.date_col,
        "description": args.desc_col,
        "amount": args.amount_col,
        "debit": args.debit_col,
        "credit": args.credit_col,
        "direction": args.direction_col,
        "date_format": args.date_format,
        "amount_sign": "debit_positive" if args.debit_positive else None,
        "delimiter": args.delimiter,
        "skip_rows": args.skip_rows,
        "encoding": args.encoding,
    }
    if mapping and any(v is not None for v in overrides.values()):
        raise CliError("use either --mapping FILE or the column flags, not both")

    status = 0
    for path in args.files:
        if not Path(path).is_file():
            raise CliError(f"file not found: {path}")
        result = etl.import_csv(
            conn, path, mapping, overrides=overrides, strict=args.strict, dry_run=args.dry_run
        )
        verb = "would insert" if result.dry_run else "inserted"
        batch = f" (batch #{result.batch_id})" if result.batch_id else ""
        _out(
            f"{result.source_name}: read {result.rows_read}, {verb} {result.rows_inserted}, "
            f"skipped {result.rows_duplicate} duplicate(s), rejected {result.rows_rejected}"
            f"{batch}"
        )
        valid = result.rows_read - result.rows_rejected
        _out(f"  auto-categorised {result.categorized} of {valid} valid row(s) by rules")
        for err in result.errors[:10]:
            _out(f"  line {err.line_no}: {err.message}")
        if len(result.errors) > 10:
            _out(f"  ... and {len(result.errors) - 10} more rejected row(s)")
        if result.errors:
            status = 1 if args.fail_on_reject else status
    return status


def cmd_batches(args: argparse.Namespace) -> int:
    conn = _open(args)
    if args.batches_cmd == "undo":
        removed = etl.undo_batch(conn, args.id)
        _out(f"removed import batch #{args.id} and its {removed} transaction(s)")
        return 0
    rows = q.list_batches(conn)
    if not rows:
        _out("no imports yet")
        return 0
    _out(
        render_table(
            ["#", "File", "Imported (UTC)", "Read", "New", "Dupes", "Rejected", "Present"],
            [
                [
                    r["id"],
                    r["source_name"],
                    r["imported_at"],
                    r["rows_read"],
                    r["rows_inserted"],
                    r["rows_duplicate"],
                    r["rows_rejected"],
                    r["rows_present"],
                ]
                for r in rows
            ],
            align="rllrrrrr",
        )
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = _open(args)
    flt = q.TxnFilter(
        month=q.parse_month(args.month) if args.month else None,
        date_from=args.date_from,
        date_to=args.date_to,
        category=args.category,
        uncategorized=args.uncategorized,
        search=args.search,
        min_paise=rupees_to_paise(args.min) if args.min else None,
        max_paise=rupees_to_paise(args.max) if args.max else None,
        direction=args.direction,
        limit=None if args.all else args.limit,
        newest_first=not args.oldest_first,
    )
    rows = q.list_transactions(conn, flt)
    if not rows:
        _out("no matching transactions")
        return 0
    table_rows = []
    total = 0
    for r in rows:
        signed = -r["amount_paise"] if r["direction"] == "debit" else r["amount_paise"]
        total += signed
        tag = {"rule": "", "manual": " *"}.get(r["categorized_by"] or "", "")
        table_rows.append(
            [
                r["id"],
                r["txn_date"],
                r["description"],
                (r["category"] or "Uncategorized") + tag,
                _money(signed),
            ]
        )
    _out(
        render_table(
            ["#", "Date", "Description", "Category", "Amount"],
            table_rows,
            align="rlllr",
            footer=["", "", f"{len(rows)} transaction(s)", "net", _money(total)],
        )
    )
    if any(r["categorized_by"] == "manual" for r in rows):
        _out("* categorised manually (rules will not override)")
    return 0


def cmd_categories(args: argparse.Namespace) -> int:
    conn = _open(args)
    if args.categories_cmd == "add":
        q.add_category(conn, args.name, args.kind)
        _out(f"added {args.kind} category {args.name!r}")
        return 0
    if args.categories_cmd == "delete":
        q.delete_category(conn, args.name)
        _out(f"deleted category {args.name!r}; its transactions are now uncategorised")
        return 0
    rows = q.list_categories(conn)
    _out(
        render_table(
            ["#", "Category", "Kind", "Transactions", "Rules"],
            [[r["id"], r["name"], r["kind"], r["txn_count"], r["rule_count"]] for r in rows],
            align="rllrr",
        )
    )
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    conn = _open(args)
    if args.rules_cmd == "add":
        rule_id = categorize.add_rule(
            conn,
            args.pattern,
            args.category,
            match_type="regex" if args.regex else "keyword",
            priority=args.priority,
        )
        _out(f"added rule #{rule_id}; run `expense categorize` to apply it to existing rows")
        return 0
    if args.rules_cmd == "delete":
        if not categorize.delete_rule(conn, args.id):
            raise CliError(f"no rule with id {args.id}")
        _out(f"deleted rule #{args.id}")
        return 0
    rules = categorize.load_rules(conn)
    if args.rules_cmd == "test":
        hit = categorize.match(rules, etl.normalize_description(args.description))
        if hit is None:
            _out("no rule matches -> Uncategorized")
        else:
            _out(
                f"rule #{hit.id} ({hit.match_type} {hit.pattern!r}, priority {hit.priority})"
                f" -> {hit.category}"
            )
        return 0
    _out(
        render_table(
            ["#", "Priority", "Type", "Pattern", "Category"],
            [[r.id, r.priority, r.match_type, r.pattern, r.category] for r in rules],
            align="rrlll",
        )
    )
    return 0


def cmd_categorize(args: argparse.Namespace) -> int:
    conn = _open(args)
    if args.txn is not None:
        if args.clear:
            categorize.set_category(conn, args.txn, None)
            _out(f"transaction #{args.txn} is now uncategorised")
        elif args.category:
            categorize.set_category(conn, args.txn, args.category)
            _out(f"transaction #{args.txn} -> {args.category} (manual)")
        else:
            raise CliError("--txn needs --category NAME or --clear")
        return 0
    result = categorize.apply_rules(conn, include_rule_assigned=args.all, dry_run=args.dry_run)
    verb = "would change" if args.dry_run else "changed"
    _out(
        f"examined {result.examined} transaction(s), {verb} {result.changed}, "
        f"{result.uncategorized} still uncategorised"
    )
    return 0


def _months_for_budget(args: argparse.Namespace) -> list[str]:
    start = q.parse_month(args.month) if args.month else date.today().strftime("%Y-%m")
    if args.through:
        end = q.parse_month(args.through)
        if end < start:
            raise CliError("--through must not be before --month")
        return q.month_range(start, end)
    return [start]


def cmd_budget(args: argparse.Namespace) -> int:
    conn = _open(args)
    if args.budget_cmd == "set":
        months = _months_for_budget(args)
        paise = rupees_to_paise(args.amount)
        q.set_budget(conn, args.category, paise, months)
        span = months[0] if len(months) == 1 else f"{months[0]}..{months[-1]}"
        _out(f"budget for {args.category}: {_money(paise)} per month ({span})")
        return 0
    if args.budget_cmd == "delete":
        if not q.delete_budget(conn, args.category, args.month):
            raise CliError(f"no budget for {args.category!r} in {args.month}")
        _out(f"deleted {args.category} budget for {args.month}")
        return 0
    rows = q.list_budgets(conn, args.month)
    if not rows:
        _out("no budgets set")
        return 0
    _out(
        render_table(
            ["Month", "Category", "Budget"],
            [[r["month"], r["category"], _money(r["amount_paise"])] for r in rows],
            align="llr",
        )
    )
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    conn = _open(args)
    month = _resolve_month(conn, args.month)
    ov = q.month_overview(conn, month)
    rows = q.summary_rows(conn, month)

    change = ov.change_vs_prev_pct
    vs = (
        f"  ({change:+.1f}% vs {_month_name(q.add_months(month, -1))})"
        if change is not None
        else ""
    )
    _out(f"Summary for {_month_name(month)}")
    _out(f"  Spent         {_money(ov.spent_paise)}{vs}")
    _out(f"  Income        {_money(ov.income_paise)}")
    _out(f"  Net           {_money(ov.net_paise)}")
    _out(f"  Transactions  {ov.txn_count}")
    if ov.budget_total_paise:
        pct = ov.budgeted_spent_paise * 100 / ov.budget_total_paise
        _out(
            f"  Budgets       {_money(ov.budgeted_spent_paise)} of "
            f"{_money(ov.budget_total_paise)} used ({pct:.0f}%)"
        )
    _out()
    if not rows:
        _out("no spending or budgets this month")
        return 0
    _out(
        render_table(
            ["Category", "Spent", "Budget", "Used", "Remaining", "Status"],
            [
                [
                    s.category,
                    _money(s.spent_paise),
                    _money(s.budget_paise),
                    "" if s.pct_used is None else f"{s.pct_used:.0f}%",
                    _money(s.remaining_paise),
                    s.status,
                ]
                for s in rows
            ],
            align="lrrrrl",
            footer=[
                "Total",
                _money(ov.spent_paise),
                _money(ov.budget_total_paise or None),
                "",
                "",
                "",
            ],
        )
    )
    over = [s for s in rows if s.over_budget]
    if over:
        _out()
        names = ", ".join(f"{s.category} (+{_money(-(s.remaining_paise or 0))})" for s in over)
        _out(f"OVER BUDGET in {len(over)} categor{'y' if len(over) == 1 else 'ies'}: {names}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from expense_dashboard.charts import generate_report  # lazy: matplotlib import is slow

    conn = _open(args)
    month = _resolve_month(conn, args.month)
    paths = generate_report(conn, args.out, month, months=args.months, note=args.note)
    _out(f"wrote {len(paths)} chart(s) for {_month_name(month)}:")
    for p in paths:
        _out(f"  {p}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from expense_dashboard import explain as ex

    conn = _open(args)
    queries = ex.explain_queries(conn)
    if args.query:
        queries = [x for x in queries if x.name == args.query]
        if not queries:
            raise CliError(f"unknown query {args.query!r}")
    failures = 0
    for query in queries:
        result = ex.explain(conn, query)
        ok = result.uses_expected_index
        failures += not ok
        _out(f"== {query.name}: {query.purpose}")
        _out(result.render_tree())
        _out(f"   expected index {query.expected_index}: {'USED' if ok else 'NOT USED'}")
        _out()
    if args.benchmark:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        _out(f"Benchmark on {count} transactions, median of {args.repeat} runs:")
        rows = []
        for query in queries:
            b = ex.benchmark(conn, query, repeat=args.repeat)
            rows.append(
                [b.name, f"{b.indexed_ms:.3f}", f"{b.full_scan_ms:.3f}", f"{b.speedup:.1f}x"]
            )
        _out(
            render_table(
                ["Query", "Indexed (ms)", "NOT INDEXED (ms)", "Speed-up"], rows, align="lrrr"
            )
        )
    return 1 if failures else 0


def cmd_demo(args: argparse.Namespace) -> int:
    from expense_dashboard import demo

    db_path = Path(args.db)
    if db_path.exists():
        if not args.force:
            raise CliError(f"{db_path} already exists; pass --force to replace it with demo data")
        db_path.unlink()
    end_month = (
        q.parse_month(args.end_month)
        if args.end_month
        else q.add_months(date.today().strftime("%Y-%m"), -1)
    )
    conn = _connect(args, db_path)
    db.migrate(conn)
    seed_defaults(conn)
    results = demo.load_demo(
        conn, args.data_dir, seed=args.seed, end_month=end_month, months=args.months
    )
    _out(demo.SYNTHETIC_BANNER)
    for r in results:
        _out(f"  imported {r.rows_inserted} rows from {Path(args.data_dir) / r.source_name}")
    _out(f"  set {len(demo.DEMO_BUDGETS)} monthly budgets for {args.months} month(s)")
    _out(f"  database: {db_path.resolve()}")
    if args.report:
        from expense_dashboard.charts import generate_report

        for p in generate_report(
            conn, args.report, end_month, months=args.months, note="Synthetic demo data"
        ):
            _out(f"  chart: {p}")
    _out(f"next: expense --db {args.db} summary")
    return 0


# --------------------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expense",
        description="Personal expense dashboard: SQLite storage, CSV import, budgets, charts.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--db",
        default=os.environ.get("EXPENSE_DB", DEFAULT_DB),
        help=f"SQLite database file (default: $EXPENSE_DB or ./{DEFAULT_DB})",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("init", help="create or migrate the database")
    p.add_argument("--no-defaults", action="store_true", help="skip starter categories/rules")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="record one transaction manually")
    p.add_argument("amount", help="amount in rupees, e.g. 249 or 1,299.50")
    p.add_argument("description")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--category", help="category name (default: apply rules)")
    p.add_argument("--income", action="store_true", help="record a credit instead of a debit")
    p.add_argument("--note")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("delete", help="delete one transaction by id")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("import", help="import bank/card statement CSV files")
    p.add_argument("files", nargs="+")
    p.add_argument("--mapping", help="JSON column-mapping file (see examples/)")
    p.add_argument("--date-col")
    p.add_argument("--desc-col")
    p.add_argument("--amount-col", help="single signed amount column")
    p.add_argument("--debit-col")
    p.add_argument("--credit-col")
    p.add_argument(
        "--direction-col", help="Dr/Cr column that gives the sign of an unsigned amount column"
    )
    p.add_argument("--date-format", help="strptime format, e.g. %%m/%%d/%%Y (default: auto)")
    p.add_argument(
        "--debit-positive",
        action="store_true",
        help="in a signed amount column, positive numbers are spending (credit-card exports)",
    )
    p.add_argument("--delimiter")
    p.add_argument("--skip-rows", type=int, help="preamble lines before the header row")
    p.add_argument("--encoding")
    p.add_argument("--strict", action="store_true", help="abort the file if any row is invalid")
    p.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    p.add_argument("--fail-on-reject", action="store_true", help="exit 1 if any row was rejected")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("batches", help="list or undo imports")
    bsub = p.add_subparsers(dest="batches_cmd", required=True)
    bsub.add_parser("list")
    b = bsub.add_parser("undo", help="delete a batch and the rows it inserted")
    b.add_argument("id", type=int)
    p.set_defaults(func=cmd_batches)

    p = sub.add_parser("list", help="list transactions with filters")
    p.add_argument("--month", help="YYYY-MM")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD inclusive")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD inclusive")
    p.add_argument("--category")
    p.add_argument("--uncategorized", action="store_true")
    p.add_argument("--search", help="substring of the description")
    p.add_argument("--min", help="minimum amount in rupees")
    p.add_argument("--max", help="maximum amount in rupees")
    direction = p.add_mutually_exclusive_group()
    direction.add_argument("--debits", dest="direction", action="store_const", const="debit")
    direction.add_argument("--credits", dest="direction", action="store_const", const="credit")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--all", action="store_true", help="no row limit")
    p.add_argument("--oldest-first", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("categories", help="list, add or delete categories")
    csub = p.add_subparsers(dest="categories_cmd", required=True)
    csub.add_parser("list")
    c = csub.add_parser("add")
    c.add_argument("name")
    c.add_argument("--kind", choices=["expense", "income", "transfer"], default="expense")
    c = csub.add_parser("delete")
    c.add_argument("name")
    p.set_defaults(func=cmd_categories)

    p = sub.add_parser("rules", help="manage auto-categorisation rules")
    rsub = p.add_subparsers(dest="rules_cmd", required=True)
    rsub.add_parser("list")
    r = rsub.add_parser("add", help="add a keyword (default) or --regex rule")
    r.add_argument("pattern")
    r.add_argument("category")
    r.add_argument("--regex", action="store_true")
    r.add_argument("--priority", type=int, default=50, help="higher wins (default 50)")
    r = rsub.add_parser("delete")
    r.add_argument("id", type=int)
    r = rsub.add_parser("test", help="show which rule a description would match")
    r.add_argument("description")
    p.set_defaults(func=cmd_rules)

    p = sub.add_parser("categorize", help="apply rules, or set one transaction's category")
    p.add_argument("--all", action="store_true", help="also re-evaluate rule-assigned rows")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--txn", type=int, help="transaction id to categorise manually")
    p.add_argument("--category", help="with --txn: category name")
    p.add_argument("--clear", action="store_true", help="with --txn: make it uncategorised")
    p.set_defaults(func=cmd_categorize)

    p = sub.add_parser("budget", help="set and list monthly budgets")
    bsub = p.add_subparsers(dest="budget_cmd", required=True)
    b = bsub.add_parser("set")
    b.add_argument("category")
    b.add_argument("amount", help="monthly limit in rupees")
    b.add_argument("--month", help="YYYY-MM (default: current month)")
    b.add_argument("--through", help="YYYY-MM: also set every month up to this one")
    b = bsub.add_parser("list")
    b.add_argument("--month")
    b = bsub.add_parser("delete")
    b.add_argument("category")
    b.add_argument("--month", required=True)
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("summary", help="month totals and spending vs budget per category")
    p.add_argument("--month", help="YYYY-MM (default: latest month with data)")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("report", help="write PNG charts")
    p.add_argument("--month", help="YYYY-MM (default: latest month with data)")
    p.add_argument("--months", type=int, default=12, help="trend window (default 12)")
    p.add_argument("--out", default="reports", help="output directory (default ./reports)")
    p.add_argument("--note", help="small footnote printed on every chart")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("explain", help="show EXPLAIN QUERY PLAN for the app's main queries")
    p.add_argument("--query", help="only this query name")
    p.add_argument("--benchmark", action="store_true", help="time indexed vs NOT INDEXED")
    p.add_argument("--repeat", type=int, default=30)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("demo", help="build a database of SYNTHETIC demo data")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--end-month", help="last month to generate (default: previous month)")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--data-dir", default="demo_data", help="where to write the synthetic CSVs")
    p.add_argument("--report", metavar="DIR", help="also write the report charts here")
    p.add_argument("--force", action="store_true", help="replace an existing database file")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # never crash on a console that cannot encode a character; ₹ falls back to Rs.
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    args.connections = []
    try:
        return int(args.func(args))
    except (
        CliError,
        db.DatabaseNotInitializedError,
        etl.MappingError,
        etl.ImportAbortedError,
        categorize.RuleError,
        AmountError,
        ValueError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.IntegrityError as exc:
        print(f"error: rejected by a database constraint: {exc}", file=sys.stderr)
        return 1
    finally:
        for conn in args.connections:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
