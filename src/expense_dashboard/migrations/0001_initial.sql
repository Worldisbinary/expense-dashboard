-- 0001_initial.sql
-- Core schema for the Personal Expense Dashboard.
--
-- Conventions
--   * Money is stored as INTEGER paise (1 rupee = 100 paise). Floats never touch the
--     database, so SUM() is exact and there is no 0.1 + 0.2 drift.
--   * Dates are ISO-8601 TEXT ('YYYY-MM-DD'). ISO strings sort lexicographically in
--     chronological order, so a B-tree index on the column supports range scans
--     (txn_date >= '2026-03-01' AND txn_date < '2026-04-01').
--   * Every foreign key states its ON DELETE behaviour explicitly. PRAGMA foreign_keys
--     is switched on per connection in db.connect() (SQLite defaults it to OFF).

-- ---------------------------------------------------------------------------
-- categories: the budgeting dimension.
--   kind = 'expense'  counts towards spending and budgets
--          'income'   salary, interest, refunds you want to track as income
--          'transfer' money moving between your own pots (SIP, credit-card bill,
--                     self transfer); excluded from spending to avoid double counting
-- ---------------------------------------------------------------------------
CREATE TABLE categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL COLLATE NOCASE UNIQUE
                        CHECK (length(trim(name)) > 0),
    kind        TEXT    NOT NULL DEFAULT 'expense'
                        CHECK (kind IN ('expense', 'income', 'transfer')),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- import_batches: one audit row per CSV import (including no-op re-imports).
-- ---------------------------------------------------------------------------
CREATE TABLE import_batches (
    id              INTEGER PRIMARY KEY,
    source_name     TEXT    NOT NULL CHECK (length(source_name) > 0),
    file_sha256     TEXT    NOT NULL CHECK (length(file_sha256) = 64),
    imported_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    rows_read       INTEGER NOT NULL DEFAULT 0 CHECK (rows_read >= 0),
    rows_inserted   INTEGER NOT NULL DEFAULT 0 CHECK (rows_inserted >= 0),
    rows_duplicate  INTEGER NOT NULL DEFAULT 0 CHECK (rows_duplicate >= 0),
    rows_rejected   INTEGER NOT NULL DEFAULT 0 CHECK (rows_rejected >= 0),
    mapping_json    TEXT,
    -- the counters must reconcile: every row read is inserted, a duplicate, or rejected
    CHECK (rows_inserted + rows_duplicate + rows_rejected = rows_read)
);

-- ---------------------------------------------------------------------------
-- rules: user-editable auto-categorisation rules.
--   keyword: case-insensitive whole-token match ('ola' matches 'OLA CABS' but not 'GRANOLA')
--   regex:   Python re.search with IGNORECASE (validated in Python before insert)
--   Higher priority wins; ties go to the older rule (lower id).
-- ---------------------------------------------------------------------------
CREATE TABLE rules (
    id          INTEGER PRIMARY KEY,
    pattern     TEXT    NOT NULL CHECK (length(trim(pattern)) > 0),
    match_type  TEXT    NOT NULL DEFAULT 'keyword'
                        CHECK (match_type IN ('keyword', 'regex')),
    category_id INTEGER NOT NULL REFERENCES categories (id) ON DELETE CASCADE,
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (pattern, match_type)
);

-- ---------------------------------------------------------------------------
-- transactions: the fact table.
-- ---------------------------------------------------------------------------
CREATE TABLE transactions (
    id              INTEGER PRIMARY KEY,
    -- date(x) returns NULL for garbage and normalises out-of-range days, so this only
    -- holds for real calendar dates in canonical ISO form. It must be IS, not '=':
    -- a CHECK whose result is NULL counts as passing, and 'x' = NULL is NULL.
    txn_date        TEXT    NOT NULL CHECK (date(txn_date) IS txn_date),
    description     TEXT    NOT NULL CHECK (length(trim(description)) > 0),
    amount_paise    INTEGER NOT NULL CHECK (amount_paise > 0),
    direction       TEXT    NOT NULL CHECK (direction IN ('debit', 'credit')),
    category_id     INTEGER REFERENCES categories (id) ON DELETE SET NULL,
    rule_id         INTEGER REFERENCES rules (id) ON DELETE SET NULL,
    categorized_by  TEXT    CHECK (categorized_by IN ('rule', 'manual')),
    source          TEXT    NOT NULL CHECK (source IN ('import', 'manual')),
    -- deleting an import batch rolls its rows back out ("expense batches undo")
    batch_id        INTEGER REFERENCES import_batches (id) ON DELETE CASCADE,
    dedupe_hash     TEXT    NOT NULL CHECK (length(dedupe_hash) = 64),
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (source = 'manual' OR batch_id IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- budgets: one monthly limit per (month, category).
-- ---------------------------------------------------------------------------
CREATE TABLE budgets (
    id           INTEGER PRIMARY KEY,
    month        TEXT    NOT NULL
                         CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
                                AND substr(month, 6, 2) BETWEEN '01' AND '12'),
    category_id  INTEGER NOT NULL REFERENCES categories (id) ON DELETE CASCADE,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    -- month first: the summary looks budgets up with WHERE month = ?, and this
    -- UNIQUE constraint's implicit index doubles as that lookup index and as the
    -- ON CONFLICT target for the upsert in budget set
    UNIQUE (month, category_id)
);

-- ---------------------------------------------------------------------------
-- Indexes, each chosen for a specific query (see queries.EXPLAIN_QUERIES and
-- `expense explain`).
-- ---------------------------------------------------------------------------

-- Idempotent import: INSERT OR IGNORE relies on this unique index to drop re-imported rows.
CREATE UNIQUE INDEX ux_transactions_dedupe_hash ON transactions (dedupe_hash);

-- Date-range aggregation: monthly trend, category totals, daily spending, list --from/--to.
-- txn_date leads, so a date range is one B-tree seek plus a sequential walk. The
-- trailing columns make it a COVERING index for the aggregate queries (they only
-- read direction, category_id and amount_paise), so SQLite never has to jump from
-- the index back into the table per row. Measured on 200k rows (scripts/
-- benchmark_indexes.py): the 12-month trend went from 108 ms with a plain
-- (txn_date) index, slower than a 44 ms full scan, to 9 ms with this one.
CREATE INDEX idx_transactions_date
    ON transactions (txn_date, direction, category_id, amount_paise);

-- Per-category lookups inside a month: budget adherence join, list --category --month.
-- Equality column first, range column second, so one seek satisfies
-- category_id = ? AND txn_date >= ? AND txn_date < ?. direction and amount_paise make
-- the budget join covering. The leading column doubles as the FK index for category_id.
CREATE INDEX idx_transactions_category_date
    ON transactions (category_id, txn_date, direction, amount_paise);

-- Foreign-key child index: without it, deleting a batch (ON DELETE CASCADE) would
-- full-scan transactions to find the children.
CREATE INDEX idx_transactions_batch ON transactions (batch_id);

-- Foreign-key child index for rules.category_id (cascade on category delete).
CREATE INDEX idx_rules_category ON rules (category_id);
