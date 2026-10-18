# Interview Guide: Personal Expense Dashboard

Prep notes for discussing this project in interviews: the pitch, how data flows, every
design decision with its trade-off, likely questions with model answers, and the weak
spots to own before someone else finds them.

---

## 1. The 2-minute pitch

> "I built a command-line personal finance dashboard in Python on SQLite. You point it at
> the CSV statements your bank and credit card already give you. It runs them through an
> ETL pipeline that parses Indian rupee formats and day-first dates, normalises the
> descriptions, and gives every row a stable SHA-256 identity. Importing the same
> statement twice, or two overlapping ones, never creates duplicates. The unique index on
> that hash with `INSERT OR IGNORE` enforces it inside the database, and each file loads
> in a single transaction with `executemany`, so an import is all-or-nothing and logged
> as a batch you can undo.
>
> Transactions are auto-categorised by keyword and regex rules stored in the database,
> which the user can edit from the CLI. On top of that sit indexed SQL aggregates (monthly
> trend, per-category totals, daily spend, and a budget-versus-actual join) computed live,
> so there are no summary tables to go stale. Matplotlib renders those into four PNG
> reports with the headless Agg backend.
>
> The part I'm proudest of is that the indexes are justified, not decorative. An
> `explain` command runs `EXPLAIN QUERY PLAN` on the app's real queries, and the tests fail
> if any of them stops using its index. When I benchmarked on 200k rows, I found my plain
> date index was actually *slower* than a full table scan for a 12-month report. I
> switched to a covering index and that query got about 8x faster. Everything has 256
> tests at 99% coverage, and CI runs on Python 3.11 to 3.13."

Short version (30 s): *CSV statements in, idempotent ETL into SQLite, rule-based
categorisation, indexed SQL aggregates for budgets and trends, Matplotlib reports out.
The indexes are proven with EXPLAIN QUERY PLAN and enforced by tests.*

---

## 2. End-to-end data flow

Follow one row, `02/03/2026,UPI/SWIGGY/612345678902,412.00,,...`, from file to chart.

1. **CLI** (`cli.py`): `expense import statement.csv` parses arguments, opens the database
   with `db.connect()` (sets `PRAGMA foreign_keys = ON`), and checks the schema version
   in `PRAGMA user_version`.
2. **Extract** (`etl.extract`): reads the file bytes, decodes with `utf-8-sig` (drops an
   Excel BOM), skips preamble lines, and feeds `csv.DictReader`. Blank and footer lines
   are dropped. Each row keeps its physical line number for error messages.
3. **Map columns** (`etl.detect_mapping`): headers are normalised (`"Withdrawal Amt."`
   becomes `"withdrawal amt"`) and matched against alias lists. CLI flags or a JSON
   mapping file override that.
4. **Transform** (`etl.transform_row`):
   - `parse_date("02/03/2026")` gives `2026-03-02`. ISO is tried first, then Indian
     day-first formats. US month-first is never guessed.
   - `parse_amount("412.00")` goes through `Decimal` to `41200` paise. Split columns
     become a signed value: debit is negative, credit positive.
   - The description is whitespace-collapsed.
5. **Identity** (`etl.dedupe_key`): `sha256("2026-03-02|-41200|upi/swiggy/612345678902|0")`.
   The trailing `0` is the occurrence counter for identical rows in one file.
6. **Categorise** (`categorize.match`): the rules are loaded once, compiled, and sorted by
   priority. The keyword rule `swiggy` matches, so the row becomes Food Delivery,
   `rule_id = 10` (with the default rules), `categorized_by = 'rule'`.
7. **Load** (`etl.load`): `BEGIN IMMEDIATE`, then insert an `import_batches` row, then
   `executemany(INSERT OR IGNORE ...)` for all rows, then `UPDATE` the batch counters,
   then `COMMIT`. `cursor.rowcount` after `executemany` is the number actually inserted;
   the rest were duplicates. Any exception means `ROLLBACK`.
8. **Query** (`queries.py`): `expense summary` runs `CATEGORY_TOTALS_SQL` for the month.
   That is a range seek on the covering `idx_transactions_date`, plus a primary-key
   lookup of the category, `GROUP BY category_id`. It then runs `BUDGET_STATUS_SQL`, a
   seek on the budgets unique index plus a seek on `idx_transactions_category_date`
   per budgeted category. The `BudgetStatus` dataclass computes % used, remaining, and
   the OK/WARN/OVER flag in Python.
9. **Render**: `tables.render_table` for the terminal, or `charts.generate_report`, which
   runs the same queries, builds Matplotlib `Figure`s on Agg, and saves PNGs.

---

## 3. Design decisions: trade-offs and alternatives

| Decision | Why | Trade-off | Alternative considered |
|---|---|---|---|
| **SQLite** | Single user, offline, zero setup, ships with Python; real SQL, ACID, FKs, B-trees | One writer at a time; no network access; fewer types | PostgreSQL (overkill: a server for one user), CSV/JSON files (no constraints, transactions or indexes), pandas (in-memory, no durability) |
| **Integer paise** | Exact sums; no float drift | Must convert for display; division needs care | `REAL` (drifts), `DECIMAL` as TEXT (SQLite has no native decimal type, so SUM would be wrong) |
| **ISO TEXT dates** | Sort chronologically, so index range scans work; readable | Need a CHECK to validate | Unix epoch INTEGER (compact but unreadable; needs conversion for every GROUP BY month) |
| **Half-open month ranges** | No 28/30/31-day bugs; index-friendly | Slightly less obvious than BETWEEN | `strftime('%Y-%m', txn_date) = ?` (a function on the column defeats the index) |
| **Covering indexes** | Aggregates are answered from the index alone; measured 8x win | Bigger indexes, a little slower inserts | Plain `(txn_date)` (measured slower than a full scan at 10% selectivity), summary tables (stale data and more write logic) |
| **Equality-then-range composite** | One seek for `category_id = ? AND date range` | Can't serve date-only queries, hence a second index | `(txn_date, category_id)`: the range on the first column stops the second from being used for seeking |
| **Content hash + `INSERT OR IGNORE`** | Idempotent re-imports; the database enforces uniqueness, so there is no race | Can't detect a bank editing a narration; identical-row edge case | Bank reference numbers (not in every export), check-then-insert in Python (slower, racy) |
| **Occurrence counter in the hash** | Keeps genuinely identical rows | Partial-day overlaps can misnumber | Dropping identical rows (silently loses real spending) |
| **One transaction + `executemany`** | Atomic import; about 400x faster than autocommitted inserts | A huge file is one big transaction (fine for SQLite at this scale) | Row-by-row commits (slow; a crash leaves a half-import) |
| **Batch audit table, ON DELETE CASCADE** | Provenance, plus a one-command undo | Extra table and FK index | No audit (can't answer "where did this row come from?") |
| **Rules in the DB, priority-ordered** | User-editable without code changes; deterministic | First match wins, no scoring; descriptions only | ML classifier (needs labelled data, not explainable), hard-coded dict (needs a code edit) |
| **Keyword = whole-token regex** | `ola` won't hit `GRANOLA` | Can't match a keyword glued to other letters | Plain substring (false positives), full tokenizer (overkill) |
| **`kind` = expense/income/transfer** | Card bill payments and SIPs aren't spending; no double counting | Users must categorise transfers correctly | Netting accounts against each other (needs account modelling) |
| **Explicit `BEGIN IMMEDIATE`** | Visible transaction boundaries; takes the write lock up front | More code than `with conn:` | sqlite3's implicit transactions (BEGIN timing is hidden; behaviour changed across Python versions) |
| **`PRAGMA user_version` migrations** | No bookkeeping table; atomic DDL plus version bump | Linear history only | Alembic (needs SQLAlchemy; heavy for one file) |
| **Agg backend, `Figure` API** | Headless (CI, SSH, cron); no pyplot global state | No interactive windows | Plotly/HTML (another dependency; the resume says Matplotlib) |
| **argparse, hand-rolled tables** | Zero dependencies; ASCII-safe output | Less pretty than Rich | Click/Typer/Rich (nicer UX, extra installs) |

---

## 4. Likely interview questions with model answers

### SQL, indexes and the query planner

**Q1. What is an index, physically, in SQLite?**
A separate B-tree keyed by the indexed columns, with each entry ending in the table's
rowid. The table itself is a B-tree keyed by rowid. A lookup through an index is
therefore two searches, index then table, unless the index already contains every column
the query needs (a covering index).

**Q2. Why a B-tree and not a hash index?**
B-trees keep keys sorted, so they support range predicates (`txn_date >= ? AND < ?`),
`ORDER BY`, `MIN`/`MAX` and prefix matches, all at O(log n) seek plus a sequential walk.
A hash index only does equality. Almost every query here is a date range, so a B-tree is
the only option. SQLite only has B-trees anyway.

**Q3. Why is the composite index `(category_id, txn_date, ...)` and not `(txn_date, category_id)`?**
The leftmost-prefix rule. The planner can seek on columns left to right while they are
equality-constrained, and can use *one* range column after them. With `category_id`
first, `category_id = 5 AND txn_date in [Mar 1, Apr 1)` is a single contiguous slice of
the B-tree. With the date first, the range on `txn_date` consumes the seek, and
`category_id` can only be filtered row by row inside that range.

**Q4. What is a covering index, and why did you add one?**
An index that contains every column the query reads, so SQLite never touches the table;
the plan says `USING COVERING INDEX`. I benchmarked 200k rows: a 12-month trend reads
about 10% of the table. With a plain `(txn_date)` index that took 108 ms, versus 44 ms for
a full scan, because about 20k random table lookups cost more than one sequential pass.
Adding `direction, category_id, amount_paise` to the index made it about 9 to 13 ms. The
lesson: an index is not automatically faster; selectivity and access pattern decide.

**Q5. How do you know your indexes are actually used?**
`expense explain` runs `EXPLAIN QUERY PLAN` on the exact SQL templates the app executes.
`SEARCH t USING COVERING INDEX idx_transactions_date (txn_date>? AND txn_date<?)` means a
range seek; `SCAN t` would mean a full scan. Tests assert every query uses its expected
index and has no `SCAN` step. Another test drops an index and checks that the detection
fires, so the check itself is tested. CI runs `expense explain` and fails on an exit code
of 1.

**Q6. What is the difference between SCAN and SEARCH in a query plan?**
`SEARCH` uses an index key or rowid to jump to a subset of rows. `SCAN` walks the whole
table, or a whole index. `USE TEMP B-TREE FOR GROUP BY/ORDER BY` means SQLite has to
sort intermediate results, because no index provides that order.

**Q7. Why do some plans still show `USE TEMP B-TREE`?**
The monthly trend groups by `substr(txn_date,1,7)`, an expression, so index order on
`txn_date` doesn't directly satisfy the grouping. The category totals group by
`category_id` while seeking on date. The sort is over a few dozen groups, which is
trivial. I could add an expression index, but it wouldn't be worth the write cost.

**Q8. Why not wrap the column in a function, e.g. `strftime('%Y-%m', txn_date) = '2026-03'`?**
That makes the predicate non-sargable: the index stores `txn_date`, not the function's
output, so SQLite has to evaluate the function on every row, which is a full scan. A
half-open range on the raw column uses the index.

**Q9. What does `NOT INDEXED` do?**
It is a SQLite table hint that forbids index use for that table. I use it only in the
benchmark to get an honest "without index" baseline from the exact same SQL.

**Q10. Is there a cost to indexes?**
Yes. Every insert updates every index, and indexes take disk space. In my runs, bulk
insert throughput dropped only slightly (about 20.8k vs 19.7k rows/s) when the indexes
became covering. For a
read-heavy dashboard that is a good trade. For a write-heavy log it might not be. I also
avoided indexing tiny tables (rules) except where a foreign-key cascade needs it.

### Transactions, ACID, integrity

**Q11. Walk me through ACID in your import.**
*Atomicity*: `BEGIN IMMEDIATE` … `COMMIT` around the batch row, every insert and the
counters, with `ROLLBACK` on any exception. A test injects a failure at the last
statement and asserts zero rows and zero batches remain. *Consistency*: CHECK, UNIQUE and
FK constraints reject bad data; the batch table even checks that
`inserted + duplicate + rejected = read`. *Isolation*: SQLite serialises writers, and
`IMMEDIATE` takes the write lock at BEGIN, so two concurrent imports fail fast instead of
deadlocking. *Durability*: SQLite's rollback journal; the commit is on disk when
`COMMIT` returns.

**Q12. Why `BEGIN IMMEDIATE` instead of plain `BEGIN`?**
Plain `BEGIN` is deferred: it takes a read lock first and upgrades to a write lock at the
first write, and two connections doing that can each block the other's upgrade, which
gives SQLITE_BUSY mid-transaction. `IMMEDIATE` grabs the reserved write lock up front, so
conflicts surface at the start, before any work is done.

**Q13. Foreign keys in SQLite: anything surprising?**
They are off by default, per connection, for backwards compatibility. `db.connect()` runs
`PRAGMA foreign_keys = ON` on every connection, and a test asserts both the pragma and
that an orphan insert raises `IntegrityError`. The other gotcha is that the child side of
a foreign key isn't indexed automatically, so I added `idx_transactions_batch`;
otherwise `batches undo` (a cascade delete) would scan the whole transactions table.

**Q14. Tell me about a bug your tests caught.**
`CHECK (txn_date = date(txn_date))` let `'15/01/2026'` through. `date()` returns NULL
for unparseable input, `x = NULL` is NULL, and SQLite treats a CHECK that evaluates to
NULL as satisfied, the same three-valued logic as WHERE. The fix is
`date(txn_date) IS txn_date`, since `IS` is null-safe and returns 0 or 1. Another test
caught `'1.234,50'` (European format) being silently read as ₹1.23, so it is now
rejected explicitly.

### ETL, idempotency, batching

**Q15. What does idempotent mean here, and how did you get it?**
Running the same import N times leaves the database exactly as after the first run.
Each row's identity is a SHA-256 of date, signed paise, the normalised description and an
occurrence counter. A UNIQUE index on that hash plus `INSERT OR IGNORE` means the database
drops duplicates atomically. Re-imports still log a batch row (0 inserted, N duplicates),
so the audit trail is complete. Tests cover the same file, an overlapping statement with
different formatting, and identical rows within a file.

**Q16. Why hash at all, instead of a UNIQUE constraint over (date, amount, description)?**
Both would work. The hash gives one fixed-width key, which is cheaper to index than a
long description. It lets me normalise (casefold, collapse whitespace) without changing
the stored, displayed text. And it folds in the occurrence counter cleanly. The cost is
that it's opaque: you can't query by its parts. I keep the parts as real columns anyway.

**Q17. Two genuinely identical transactions on the same day. What happens?**
Within one file they get occurrence 0 and 1, so both are inserted, and re-importing the
file reproduces 0 and 1, so neither is duplicated. Manual `add` bumps the counter until
the hash is free, because if you type it twice you mean it. The edge case: statement A
contains one of the two and statement B both. Then B's first copy matches A's and the
second is new, which happens to be correct. It only breaks if a bank splits a day
inconsistently across files. That's documented as a limitation.

**Q18. `executemany` vs a loop of `execute` calls?**
Most of the win is the transaction, not the API. Measured on 2,000 rows: autocommitting
each `execute` ran at about 110 rows/s, because every commit is a journal write and
fsync. A loop inside one transaction ran at about 25k rows/s. `executemany` in one
transaction ran at about 45k rows/s, because the statement is prepared once and the
Python-to-C round trips are batched. So: always batch inside a transaction, and prefer
`executemany` for bulk loads.

**Q19. How do you count inserted vs duplicate rows if `INSERT OR IGNORE` is silent?**
For `executemany`, Python's sqlite3 sums the per-statement change counts into
`cursor.rowcount`. An ignored row changes 0 rows, so `rowcount` is exactly the number
inserted, and duplicates = parsed − inserted. Tests check those numbers against the
`import_batches` row.

**Q20. How do you handle bad rows?**
Collect, don't crash: each bad row becomes a `RowError(line_no, message)` and the good
rows still load. The batch records `rows_rejected`. `--strict` aborts the whole file
with nothing written, and `--dry-run` reports everything and writes nothing. Line numbers
account for preamble lines, so the user can find the row in Excel.

### Data modelling and normalisation

**Q21. Is the schema normalised?**
Yes, to about 3NF. Categories are their own table, referenced by id, so renaming a
category is one update. Budgets are keyed by (month, category); rules reference
categories; batches are separate from transactions. There are no derived totals stored,
so there is nothing to keep in sync. The one deliberate redundancy is
`categorized_by`/`rule_id` on transactions: provenance, not derivable data. It lets
manual overrides survive re-categorisation and lets you see why a row is categorised.

**Q22. Why no summary or aggregate tables?**
At personal scale, live aggregates on covering indexes take milliseconds even at 200k
rows. Materialised totals would need triggers or recompute logic on every insert,
update, delete and re-categorisation, and they could go stale. I'd only add them if
measurements demanded it, for example as a monthly rollup for very large histories.

**Q23. How are refunds and transfers treated?**
Categories have a `kind`. Spending is debits in `expense` categories or uncategorised
debits. Income is credits in `income` categories or uncategorised credits. Transfers
(card bill payment, SIP) are in neither, which avoids double counting card purchases. A
refund credited to an expense category is in neither, which is a known limitation: it
should arguably reduce that category's spend.

### Python, Matplotlib, testing

**Q24. Why the Agg backend?**
Agg is Matplotlib's non-interactive raster renderer (Anti-Grain Geometry). It needs no
GUI toolkit or display, so `expense report` works in CI containers, over SSH and in cron.
The default backend might try Tk or Qt and fail on a headless box. I call
`matplotlib.use("Agg")` before importing anything that draws, and I use the `Figure`
object API instead of `pyplot`, so there's no global figure registry to leak memory when
charts are generated in a loop.

**Q25. How do you test charts?**
Pixel comparisons are brittle across Matplotlib versions, so I test behaviour. All four
files are written, they are valid PNGs (magic bytes), they have the expected dimensions,
and empty states (no budgets, no spending) still render. By spying on `barh` and `plot`,
I also assert that the right *data* reaches the plot: the top-10 fold into "Other", the
budget bars sorted by % with the correct status colours, and the cumulative line ending
at the month total.

**Q26. What's your testing strategy overall?**
It's layered. Unit tests cover the pure functions: amount and date parsing (dozens of
parametrised cases), hashing, rule compilation. Database tests run against a real
temporary SQLite file, not mocks, because constraint and planner behaviour is the thing
under test. Aggregations are checked against a small hand-computed ledger (e.g. January
spend = 1,000.00 + 250.50, with the 5,000 SIP excluded as a transfer). CLI tests drive
`main(argv)` in-process for speed and coverage, plus a few true subprocess runs for exit
codes, environment variables and console encoding. Query-plan tests lock in the index
design. The result is 256 tests at 99% branch coverage, and CI runs on 3.11 to 3.13.

**Q27. Why `Decimal` when parsing, not `float`?**
`float("0.1") + float("0.2") != 0.3`. Parsing to `Decimal` and quantising to whole paise
with `ROUND_HALF_UP` gives the integer the user meant, and from there on everything is
integer arithmetic. A test literally asserts that 0.1 + 0.2 == 0.3 in paise.

**Q28. How would this scale to a multi-user web app?**
Swap SQLite for PostgreSQL (the SQL is almost entirely portable). Add a `user_id` to
every table and to the leading position of each index, put the ETL behind an upload
endpoint with a job queue, and keep the content-hash idempotency (it's database-enforced,
so it survives concurrent uploads). The chart code could stay as is, or move to a JS
charting library on the client.

---

## 5. Honest weak spots, and how to answer if asked

| Weak spot | If asked, say |
|---|---|
| **Synthetic data only**, no real bank's export tested | "The demo and README use a seeded generator, labelled as synthetic; I wouldn't publish my own statements. The importer is built around the shapes real exports have (preambles, split Dr/Cr columns, ₹ prefixes, BOMs), and adding a bank is a JSON mapping, not code." |
| **Refunds not netted** against their category | "It's a conscious simplification, and documented. The fix is to define category spend as debits minus credits for expense categories, about a two-line SQL change, but then I'd want rules to distinguish refunds from income." |
| **Dedupe edge case** with partially overlapping identical rows, or banks editing narrations | "Content hashing is the best you can do without a stable bank transaction ID. If the export has a reference-number column, I'd hash that instead." |
| **Rules: first match wins, descriptions only** | "Deterministic and explainable was the goal; `rules test` shows exactly which rule fires. Next steps would be amount and direction conditions, then maybe a learned classifier seeded from the user's manual corrections." |
| **No concurrency story** beyond SQLite's single writer | "It's a single-user CLI. `BEGIN IMMEDIATE` plus a 5 s busy timeout handles the accidental second process. Real multi-user means Postgres." |
| **Benchmarks are one laptop, random data** | "They demonstrate relative effects, index vs scan and batching vs autocommit, which are robust. The absolute milliseconds vary run to run, and I say so in the README." |
| **Charts tested by behaviour, not pixels** | "Deliberate: pixel diffs break on every Matplotlib or font update. I test the data passed to the plotting calls, the file validity and the sizes." |
| **"Real-time" wording** | "Real-time here means aggregates are computed on demand from the live table, so a summary right after an import reflects it. It's not streaming." |
| **CLI only, no UI** | "The resume entry is a CLI dashboard; the PNG reports are the visual layer. A web front end would reuse `queries.py` unchanged." |
| **`__main__.py` uncovered** | "It's two lines, exercised by subprocess tests, which coverage doesn't trace. I could enable subprocess coverage, but it wasn't worth the config." |

---

## 6. Numbers to remember

- 5 tables, 5 explicit indexes (+ 1 implicit UNIQUE index on budgets), 1 migration file.
- 6 main queries checked by `expense explain`; all `SEARCH`, none `SCAN`.
- 200k rows: 12-month trend about 108 ms with a plain index (slower than the 44 ms full
  scan), about 9 to 13 ms with the covering index. Budget join about 0.2 ms vs about
  0.5 s with `NOT INDEXED`.
- Inserts: about 110 rows/s autocommitted vs about 45k rows/s with `executemany` in one
  transaction.
- Demo: 12 months, 825 synthetic transactions, about 79% auto-categorised by the default
  rules.
- 256 tests, 99% line + branch coverage, CI on Python 3.11 / 3.12 / 3.13.
