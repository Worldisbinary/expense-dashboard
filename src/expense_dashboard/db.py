"""Connection handling, explicit transactions and schema migrations.

Design notes
------------
* Connections are opened in autocommit mode (``isolation_level=None``) and every
  write that must be atomic goes through :func:`transaction`, which issues an
  explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``. That keeps transaction
  boundaries visible in the code instead of relying on the sqlite3 module's implicit
  BEGIN heuristics.
* ``PRAGMA foreign_keys = ON`` is per-connection in SQLite (default OFF), so it is
  set here, once, for every connection the app opens.
* Migrations are numbered ``.sql`` files shipped inside the package. The applied
  version is tracked in ``PRAGMA user_version`` (an integer in the database header),
  so no bookkeeping table is needed.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class DatabaseNotInitializedError(RuntimeError):
    """Raised when a command needs the schema but ``expense init`` has not been run."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced and dict-like rows."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically: ``BEGIN IMMEDIATE`` ... ``COMMIT``, ``ROLLBACK`` on error.

    ``IMMEDIATE`` takes the write lock up front, so two concurrent writers fail fast
    with SQLITE_BUSY at BEGIN instead of deadlocking halfway through.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def available_migrations() -> list[tuple[int, str, str]]:
    """Return ``(version, filename, sql)`` for every bundled migration, in order."""
    folder = resources.files("expense_dashboard").joinpath("migrations")
    found: list[tuple[int, str, str]] = []
    for entry in folder.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migrations must be numbered 0001..N without gaps, got {versions}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations; return the filenames that were applied.

    Each migration runs in its own transaction together with the ``user_version``
    bump, so a failing migration leaves the database at the previous version.
    """
    current = schema_version(conn)
    applied: list[str] = []
    for version, name, sql in available_migrations():
        if version <= current:
            continue
        # executescript() runs the whole file; wrapping it in BEGIN/COMMIT makes the
        # DDL and the version bump atomic (SQLite DDL is transactional).
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(name)
    return applied


def require_schema(conn: sqlite3.Connection) -> None:
    """Fail with a helpful message if the database has not been initialised."""
    latest = available_migrations()[-1][0]
    current = schema_version(conn)
    if current == 0:
        raise DatabaseNotInitializedError("database is not initialised; run `expense init` first")
    if current < latest:
        raise DatabaseNotInitializedError(
            f"database schema is at version {current} but {latest} is available; "
            "run `expense init` to migrate"
        )
