"""
File I/O and persistence layer for collection management.

Owns all path constants, SQLite DB access, and JSON reads/writes.
No print() or input() calls. No imports from other coll_* modules.

Master data (users, beats, vouchers, installments) is stored in SQLite.
Staging and archive files remain as JSON/TXT on disk.
"""

import binascii
import csv
import hashlib
import json
import os
import re
import sqlite3
from collections import namedtuple
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

User = namedtuple('User', ['name', 'role', 'must_change_password'], defaults=[False])

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STAGING_DIR = ROOT_DIR / "staging"
ARCHIVE_DIR = ROOT_DIR / "archive"
PRINTS_DIR = ROOT_DIR / "prints"

_PRINT_COL_WIDTH = 28
_PRINT_COL_SEP = " | "
_PRINT_PAGE_HEIGHT = 66
_PRINT_FILE_HEADER_LINES = 3


def parse_decimal(value, default=Decimal("0")):
    """Lenient Decimal parse for report/display totals.

    Returns `default` for None/empty/non-numeric/NaN/Infinity input so a
    report screen or text sidecar still renders when bad data predates
    validation. Never use on a write path — write paths must raise.
    """
    try:
        d = Decimal(str(value or "").strip() or "0")
        return d if d.is_finite() else default
    except (InvalidOperation, ValueError):
        return default


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _db_path():
    """Return the SQLite DB path. Derived at call time so DATA_DIR patches work in tests."""
    return DATA_DIR / "collmgm.db"


def get_db():
    """Return an open sqlite3 connection with WAL mode and Row factory."""
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Schema v1 (PRAGMA user_version 1). NOT NULL on TEXT primary keys is not
# redundant — SQLite allows NULL in non-INTEGER PRIMARY KEY columns unless it
# is declared. Money columns stay TEXT (exact Decimal strings); the GLOB
# CHECK enforces the app's written format — non-empty, digits and dots only —
# which rejects '', 'nan', 'Infinity', negatives, and exponent forms.
# Numeric well-formedness and magnitude remain Python's job (CAST-based
# checks are useless here: CAST('nan' AS NUMERIC) is 0 in SQLite).
# The FK on installments.bill_no is enforced because get_db() sets
# PRAGMA foreign_keys=ON. The completed_* tables get no FK: they are
# read-only history and interrupted historical posts can have left
# legitimate orphans there.
_TABLE_DDL_V1 = {
    "users": """
    name                 TEXT PRIMARY KEY NOT NULL CHECK (name <> ''),
    role                 TEXT NOT NULL CHECK (role IN ('distributor','supervisor','salesman','system')),
    password_hash        TEXT NOT NULL DEFAULT '',
    must_change_password INTEGER NOT NULL DEFAULT 0,
    secret_question      TEXT,
    secret_answer_hash   TEXT
""",
    "beats": """
    name     TEXT PRIMARY KEY NOT NULL CHECK (name <> ''),
    salesman TEXT NOT NULL DEFAULT ''
""",
    "permissions": """
    role       TEXT NOT NULL,
    action_key TEXT NOT NULL,
    PRIMARY KEY (role, action_key)
""",
    "vouchers": """
    bill_no    TEXT PRIMARY KEY NOT NULL CHECK (bill_no <> ''),
    date       TEXT NOT NULL CHECK (date <> ''),
    amount     TEXT NOT NULL CHECK (amount <> '' AND amount NOT GLOB '*[^0-9.]*'),
    balance    TEXT NOT NULL CHECK (balance <> '' AND balance NOT GLOB '*[^0-9.]*'),
    beat       TEXT NOT NULL CHECK (beat <> ''),
    salesman   TEXT NOT NULL CHECK (salesman <> ''),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
""",
    "installments": """
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no      TEXT NOT NULL CHECK (bill_no <> '') REFERENCES vouchers(bill_no),
    date         TEXT NOT NULL CHECK (date <> ''),
    amount       TEXT NOT NULL CHECK (amount <> '' AND amount NOT GLOB '*[^0-9.]*'),
    salesman     TEXT NOT NULL CHECK (salesman <> ''),
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    payment_type TEXT NOT NULL DEFAULT 'cash' CHECK (payment_type IN ('cash','upi','check','returns')),
    payment_ref  TEXT NOT NULL DEFAULT ''
""",
    "completed_vouchers": """
    bill_no    TEXT PRIMARY KEY NOT NULL CHECK (bill_no <> ''),
    date       TEXT NOT NULL CHECK (date <> ''),
    amount     TEXT NOT NULL CHECK (amount <> '' AND amount NOT GLOB '*[^0-9.]*'),
    balance    TEXT NOT NULL CHECK (balance <> '' AND balance NOT GLOB '*[^0-9.]*'),
    beat       TEXT NOT NULL CHECK (beat <> ''),
    salesman   TEXT NOT NULL CHECK (salesman <> ''),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
""",
    "completed_installments": """
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no      TEXT NOT NULL CHECK (bill_no <> ''),
    date         TEXT NOT NULL CHECK (date <> ''),
    amount       TEXT NOT NULL CHECK (amount <> '' AND amount NOT GLOB '*[^0-9.]*'),
    salesman     TEXT NOT NULL CHECK (salesman <> ''),
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    payment_type TEXT NOT NULL DEFAULT 'cash' CHECK (payment_type IN ('cash','upi','check','returns')),
    payment_ref  TEXT NOT NULL DEFAULT ''
""",
}

_TABLE_COPY_COLUMNS = {
    "users": ["name", "role", "password_hash", "must_change_password",
              "secret_question", "secret_answer_hash"],
    "beats": ["name", "salesman"],
    "permissions": ["role", "action_key"],
    "vouchers": ["bill_no", "date", "amount", "balance", "beat", "salesman",
                 "created_by", "created_at"],
    "installments": ["id", "bill_no", "date", "amount", "salesman",
                     "created_by", "created_at", "payment_type", "payment_ref"],
    "completed_vouchers": ["bill_no", "date", "amount", "balance", "beat", "salesman",
                           "created_by", "created_at"],
    "completed_installments": ["id", "bill_no", "date", "amount", "salesman",
                               "created_by", "created_at", "payment_type", "payment_ref"],
}

_SCHEMA = "".join(
    f"CREATE TABLE IF NOT EXISTS {table} ({body});\n"
    for table, body in _TABLE_DDL_V1.items()
)

# Correction requests (added after schema v1). Deliberately NOT in
# _TABLE_DDL_V1: that dict drives the v1 constraint rebuild, which must never
# touch a table that post-dates v1. CREATE IF NOT EXISTS via init_db() covers
# fresh and already-installed DBs alike — no version bump needed for a purely
# additive table (widening the kind CHECK later is handled by
# _migrate_corrections_kinds). old_json/new_json hold the raise-time snapshot
# and the requested change as JSON (shape varies by kind). The four
# master-data kinds edit SQLite tables; 'collection_amount' edits the staged
# report's payment instead. Resolved rows are kept forever: they are the
# audit trail of correction-driven changes.
_CORRECTIONS_BODY = """
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN
                      ('installment_amount','installment_delete',
                       'installment_add','voucher_amount',
                       'collection_amount')),
    bill_no         TEXT NOT NULL CHECK (bill_no <> ''),
    report_stem     TEXT NOT NULL DEFAULT '',
    origin_stage    TEXT NOT NULL DEFAULT '',
    installment_id  INTEGER,
    old_json        TEXT,
    new_json        TEXT,
    note            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                      ('open','applied','rejected','withdrawn')),
    requested_by    TEXT NOT NULL,
    requested_at    TEXT NOT NULL,
    resolved_by     TEXT,
    resolved_at     TEXT,
    resolution_note TEXT
"""

_CORRECTIONS_COLUMNS = ["id", "kind", "bill_no", "report_stem", "origin_stage",
                        "installment_id", "old_json", "new_json", "note", "status",
                        "requested_by", "requested_at",
                        "resolved_by", "resolved_at", "resolution_note"]

_SCHEMA += f"CREATE TABLE IF NOT EXISTS corrections ({_CORRECTIONS_BODY});\n"

# Voucher amendments (added after schema v1, alongside corrections — same
# "deliberately NOT in _TABLE_DDL_V1" rationale). A raw, distributor-only
# full-state edit of one voucher + its installments, committed as one atomic
# transaction. No status column: unlike corrections there is no separate
# 'open' request phase — a row exists iff the edit committed, and it IS the
# audit trail (old_json/new_json hold the full before/after voucher +
# installments state, not a per-field diff).
_AMENDMENTS_BODY = """
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no     TEXT NOT NULL CHECK (bill_no <> ''),
    old_json    TEXT NOT NULL,
    new_json    TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    amended_by  TEXT NOT NULL,
    amended_at  TEXT NOT NULL
"""

_SCHEMA += f"CREATE TABLE IF NOT EXISTS amendments ({_AMENDMENTS_BODY});\n"

# Amendment requests (added after schema v1, alongside corrections/amendments
# — same "deliberately NOT in _TABLE_DDL_V1" rationale). A lightweight
# raise->resolve lifecycle in front of the distributor-only Voucher Amendment
# editor: unlike corrections there is no structured kind/old_json/new_json —
# the raiser just flags a bill_no with a free-text note; the distributor
# reads it and makes whatever edit is warranted through the existing raw
# editor. linked_amendment_id records which amendments row (if any) resolved
# the request when it was auto-settled rather than explicitly rejected.
_AMENDMENT_REQUESTS_BODY = """
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no             TEXT NOT NULL CHECK (bill_no <> ''),
    note                TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                          ('open','applied','rejected','withdrawn')),
    requested_by        TEXT NOT NULL,
    requested_at        TEXT NOT NULL,
    resolved_by         TEXT,
    resolved_at         TEXT,
    resolution_note     TEXT,
    linked_amendment_id INTEGER
"""

_SCHEMA += f"CREATE TABLE IF NOT EXISTS amendment_requests ({_AMENDMENT_REQUESTS_BODY});\n"

# Checks (added after schema v1, but — unlike corrections/amendments/
# amendment_requests — this IS master data, same category as
# vouchers/installments: it has a CSV counterpart (data/checks.csv, see
# _CHK_FIELDS / _migrate_csv_to_db below) and is documented in schema.md
# alongside the other master-data CSVs, not the "SQLite only" tables.
# Still deliberately NOT in _TABLE_DDL_V1 — it's a purely additive table, so
# CREATE IF NOT EXISTS via init_db() covers fresh and already-installed DBs
# alike with no version bump needed. installment_id is an audit-only
# pointer (NOT FK-enforced, mirrors corrections.installment_id): archiving a
# fully-settled voucher deletes its installments row and re-inserts under a
# new id in completed_installments, which would orphan a real FK — this
# table is bill_no-scoped for display, so that's fine. One row per check,
# created at post time for every payment_type='check' voucher; status moves
# pending -> encashed | bounced and never anywhere else. "Due in 2 days" /
# "overdue" are not stored states — computed from check_date vs today at
# query time (check_summary_counts/load_checks).
_CHECKS_BODY = """
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    installment_id  INTEGER,
    bill_no         TEXT NOT NULL CHECK (bill_no <> ''),
    beat            TEXT NOT NULL DEFAULT '',
    salesman        TEXT NOT NULL DEFAULT '',
    bank            TEXT NOT NULL CHECK (bank <> ''),
    branch          TEXT NOT NULL DEFAULT '',
    check_no        TEXT NOT NULL CHECK (check_no <> ''),
    check_date      TEXT NOT NULL CHECK (check_date <> ''),
    amount          TEXT NOT NULL CHECK (amount <> '' AND amount NOT GLOB '*[^0-9.]*'),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                      ('pending','encashed','bounced')),
    recorded_by     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    resolved_by     TEXT,
    resolved_at     TEXT,
    resolution_note TEXT
"""

_CHECKS_COLUMNS = ["id", "installment_id", "bill_no", "beat", "salesman", "bank",
                   "branch", "check_no", "check_date", "amount", "status",
                   "recorded_by", "recorded_at", "resolved_by", "resolved_at",
                   "resolution_note"]

_SCHEMA += f"CREATE TABLE IF NOT EXISTS checks ({_CHECKS_BODY});\n"


class MigrationError(ValueError):
    """Existing rows violate the new schema constraints; the migration
    refuses to run until they are repaired manually."""


def init_db():
    """Create all tables in the DB (idempotent). Creates the DB file if absent.

    Also applies one-time migrations for DBs created before a given
    column/table/constraint existed: additive backfills (beats.salesman,
    permissions) and the v1 constraint rebuild (_migrate_schema_v1).
    Raises MigrationError when existing rows violate the v1 constraints.
    """
    conn = get_db()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _backfill_beats_salesman(conn)
        _backfill_must_change_password(conn)
        _backfill_secret_question_columns(conn)
        _backfill_permissions(conn)
        _backfill_coll_print_permission(conn)
        _backfill_correction_permissions(conn)
        _backfill_amendment_permission(conn)
        _backfill_amendment_request_permissions(conn)
        _backfill_payment_type_columns(conn)
        _backfill_check_permissions(conn)
        conn.commit()
        _migrate_corrections_kinds(conn)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            _migrate_schema_v1(conn)
        _migrate_payment_type_returns(conn)
    finally:
        conn.close()


def _repair_interrupted_archive_orphans(conn):
    """Move installments whose voucher already reached completed_vouchers to
    completed_installments — debris from a historical post that was
    interrupted between archiving the voucher and its installments."""
    where = ("bill_no NOT IN (SELECT bill_no FROM vouchers)"
             " AND bill_no IN (SELECT bill_no FROM completed_vouchers)")
    conn.execute(
        "INSERT INTO completed_installments"
        " (bill_no, date, amount, salesman, created_by, created_at)"
        f" SELECT bill_no, date, amount, salesman, created_by, created_at"
        f" FROM installments WHERE {where}")
    conn.execute(f"DELETE FROM installments WHERE {where}")


def _scan_v1_violations(conn):
    """Return [(table, identifier, reason)] for rows the v1 constraints
    would reject. Read-only."""
    violations = []

    for r in conn.execute(
            "SELECT name, role FROM users WHERE name IS NULL OR name = ''"
            " OR role IS NULL"
            " OR role NOT IN ('distributor','supervisor','salesman','system')"):
        violations.append(("users", r["name"] or "<empty>", f"invalid role {r['role']!r}"))

    for r in conn.execute("SELECT name FROM beats WHERE name IS NULL OR name = ''"):
        violations.append(("beats", "<empty>", "empty name"))

    voucher_required = ("bill_no", "date", "beat", "salesman")
    installment_required = ("bill_no", "date", "salesman")
    checks = [
        ("vouchers", voucher_required, ("amount", "balance")),
        ("completed_vouchers", voucher_required, ("amount", "balance")),
        ("installments", installment_required, ("amount",)),
        ("completed_installments", installment_required, ("amount",)),
    ]
    for table, required, money in checks:
        empty = " OR ".join(f"{c} IS NULL OR {c} = ''" for c in required)
        for r in conn.execute(f"SELECT bill_no FROM {table} WHERE {empty}"):
            violations.append((table, r["bill_no"] or "<empty>", "empty required field"))
        for col in money:
            for r in conn.execute(
                    f"SELECT bill_no, {col} FROM {table}"
                    f" WHERE {col} IS NULL OR {col} = '' OR {col} GLOB '*[^0-9.]*'"):
                violations.append((table, r["bill_no"] or "<empty>",
                                   f"non-numeric {col} {r[col]!r}"))

    for r in conn.execute(
            "SELECT DISTINCT bill_no FROM installments"
            " WHERE bill_no NOT IN (SELECT bill_no FROM vouchers)"):
        violations.append(("installments", r["bill_no"] or "<empty>",
                           "no matching voucher"))

    return violations


def _migrate_schema_v1(conn):
    """Rebuild every table with the v1 constraints and stamp
    PRAGMA user_version = 1.

    SQLite cannot ALTER TABLE ADD CONSTRAINT, so each table is rebuilt in
    one transaction: CREATE {t}_v1 -> copy rows -> DROP {t} -> RENAME.
    Refuses (MigrationError, everything rolled back) while any existing row
    would violate the new constraints, listing the offending rows so the
    operator can repair data/collmgm.db and restart.
    """
    _repair_interrupted_archive_orphans(conn)
    violations = _scan_v1_violations(conn)
    if violations:
        conn.rollback()  # undo the orphan repair — the DB stays untouched
        listed = "; ".join(f"{t}[{ident}]: {reason}" for t, ident, reason in violations[:20])
        more = f" (+{len(violations) - 20} more)" if len(violations) > 20 else ""
        raise MigrationError(
            "Cannot upgrade the database schema — existing rows violate the new"
            f" constraints: {listed}{more}."
            " Fix these rows in data/collmgm.db and restart.")
    conn.commit()

    # foreign_keys can only change outside a transaction; restore it after.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            for table, body in _TABLE_DDL_V1.items():
                cols = ", ".join(_TABLE_COPY_COLUMNS[table])
                conn.execute(f"DROP TABLE IF EXISTS {table}_v1")
                conn.execute(f"CREATE TABLE {table}_v1 ({body})")
                conn.execute(f"INSERT INTO {table}_v1 ({cols}) SELECT {cols} FROM {table}")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {table}_v1 RENAME TO {table}")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationError(
                    "Foreign key check failed after the schema rebuild — migration rolled back.")
            conn.execute("PRAGMA user_version = 1")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_payment_type_returns(conn):
    """Rebuild installments/completed_installments when their payment_type
    CHECK predates the 'returns' value. Self-detecting via the stored CREATE
    SQL (SQLite cannot widen a CHECK in place). Tables with no payment_type
    CHECK at all (columns added by ALTER) are left alone. Pure widening: every
    existing row satisfies the new CHECK, and ids are copied so they survive.
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in ("installments", "completed_installments"):
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,)).fetchone()
            sql = (row["sql"] or "") if row else ""
            if "payment_type IN" not in sql or "'returns'" in sql:
                continue
            cols = ", ".join(_TABLE_COPY_COLUMNS[table])
            seq = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
            with conn:
                conn.execute(f"DROP TABLE IF EXISTS {table}_rt")
                conn.execute(f"CREATE TABLE {table}_rt ({_TABLE_DDL_V1[table]})")
                conn.execute(f"INSERT INTO {table}_rt ({cols}) SELECT {cols} FROM {table}")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {table}_rt RENAME TO {table}")
                if seq is not None:
                    # AUTOINCREMENT high-water mark: without this, ids deleted
                    # from the top of the table would be reused, and
                    # checks.installment_id (an audit pointer) could then
                    # reference a different installment.
                    cur = conn.execute(
                        "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
                        (seq[0], table))
                    if cur.rowcount == 0:
                        conn.execute(
                            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                            (table, seq[0]))
                # Scoped to this table: an unrelated pre-existing orphan
                # elsewhere must not stop the app from starting.
                if conn.execute(f"PRAGMA foreign_key_check({table})").fetchall():
                    raise MigrationError(
                        f"Foreign key check failed on {table} after widening"
                        " payment_type — migration rolled back.")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _backfill_beats_salesman(conn):
    """Add beats.salesman if missing, then fill it in from data/beats.csv by name."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(beats)").fetchall()]
    if "salesman" not in cols:
        conn.execute("ALTER TABLE beats ADD COLUMN salesman TEXT NOT NULL DEFAULT ''")
    beats_csv = DATA_DIR / "beats.csv"
    if not beats_csv.exists():
        return
    try:
        with beats_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row.get("name", "").strip()
                salesman = row.get("salesman", "").strip()
                if name and salesman:
                    conn.execute(
                        "UPDATE beats SET salesman = ? WHERE name = ? AND salesman = ''",
                        (salesman, name),
                    )
    except Exception:
        pass


def _backfill_secret_question_columns(conn):
    """Add users.secret_question / users.secret_answer_hash if missing
    (additive, nullable — existing accounts simply have no question set)."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    for col in ("secret_question", "secret_answer_hash"):
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")


def _backfill_must_change_password(conn):
    """Add users.must_change_password if missing (additive column; defaults
    to 0/false for every pre-existing row — no forced change is retroactively
    imposed on already-provisioned accounts)."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "must_change_password" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")


def _backfill_permissions(conn):
    """Additive grant of the original iteration1/2 "base" RBAC keys — the
    ones that predate every other _backfill_*_permission function below and
    so were never given their own hardcoded backfill, relying instead on
    data/permissions.csv being present at first boot. A packaged install's
    data/ directory ships no CSVs at all (only collmgm.db), so that read
    silently no-ops and these keys — including manage_users/manage_beats —
    never reach the table. Hardcoded and unconditional like its siblings
    (not gated on the table being empty) so it also heals an install that
    already picked up later permission keys via those other backfills."""
    conn.executemany(
        "INSERT OR IGNORE INTO permissions (role, action_key) VALUES (?, ?)",
        [
            ("salesman", "coll_start"),
            ("salesman", "coll_submit"),
            ("salesman", "add_vouchers"),
            ("salesman", "reports"),
            ("supervisor", "coll_start"),
            ("supervisor", "coll_approve_start"),
            ("supervisor", "coll_submit"),
            ("supervisor", "coll_approve_submit"),
            ("supervisor", "add_vouchers"),
            ("supervisor", "import_vouchers"),
            ("supervisor", "reports"),
            ("distributor", "coll_start"),
            ("distributor", "coll_approve_start"),
            ("distributor", "coll_submit"),
            ("distributor", "coll_approve_submit"),
            ("distributor", "coll_post"),
            ("distributor", "add_vouchers"),
            ("distributor", "import_vouchers"),
            ("distributor", "approve_new_vouchers"),
            ("distributor", "post_new_vouchers"),
            ("distributor", "reports"),
            ("distributor", "manage_users"),
            ("distributor", "manage_beats"),
        ],
    )


def _backfill_coll_print_permission(conn):
    """One-time additive grant of coll_print for DBs seeded before the key existed."""
    conn.executemany(
        "INSERT OR IGNORE INTO permissions (role, action_key) VALUES (?, ?)",
        [("supervisor", "coll_print"), ("distributor", "coll_print")],
    )


def _backfill_correction_permissions(conn):
    """Additive grant of the correction-request keys for DBs seeded before
    they existed. raise_correction doubles as the view permission for the
    corrections list; apply_correction gates the distributor's Apply/Reject
    on the master-data kinds (collection_amount is gated on the existing
    coll_approve_submit key instead)."""
    conn.executemany(
        "INSERT OR IGNORE INTO permissions (role, action_key) VALUES (?, ?)",
        [("supervisor", "raise_correction"), ("distributor", "raise_correction"),
         ("distributor", "apply_correction")],
    )


def _backfill_amendment_permission(conn):
    """One-time additive grant of amend_voucher for DBs seeded before it existed."""
    conn.executemany(
        "INSERT OR IGNORE INTO permissions (role, action_key) VALUES (?, ?)",
        [("distributor", "amend_voucher")],
    )


def _backfill_amendment_request_permissions(conn):
    """Additive grant of the amendment-request keys for DBs seeded before
    they existed: raise_amendment_request (supervisor, salesman) doubles as
    the view permission for the amendment-requests list, same pattern as
    raise_correction. Also widens raise_correction to salesman, so a
    salesman reaching the Correction Requests flow can use the
    "raise an amendment request instead" cross-link there too."""
    conn.executemany(
        "INSERT OR IGNORE INTO permissions (role, action_key) VALUES (?, ?)",
        [("supervisor", "raise_amendment_request"),
         ("salesman", "raise_amendment_request"),
         ("salesman", "raise_correction")],
    )


def _backfill_payment_type_columns(conn):
    """Add payment_type/payment_ref to installments and completed_installments
    if missing (additive columns; existing rows default to 'cash'/'' — no
    historical payment is retroactively reclassified)."""
    for table in ("installments", "completed_installments"):
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "payment_type" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN payment_type TEXT NOT NULL DEFAULT 'cash'")
        if "payment_ref" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN payment_ref TEXT NOT NULL DEFAULT ''")


def _backfill_check_permissions(conn):
    """Additive grant of the check-tracking keys for DBs seeded before they
    existed. view_checks (everyone) covers the menu banner and the Checks
    screen; resolve_check (distributor only) gates the mark-encashed/bounced
    actions."""
    conn.executemany(
        "INSERT OR IGNORE INTO permissions (role, action_key) VALUES (?, ?)",
        [("salesman", "view_checks"), ("supervisor", "view_checks"),
         ("distributor", "view_checks"), ("distributor", "resolve_check")],
    )


def _migrate_corrections_kinds(conn):
    """Rebuild the corrections table when its kind CHECK predates a newer
    kind value. Self-detecting via the stored CREATE SQL — SQLite cannot
    widen a CHECK in place, and CREATE IF NOT EXISTS is a no-op on DBs that
    already made the table with the old constraint. Pure widening: every
    existing row satisfies the new CHECK, so the copy cannot fail."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'corrections'"
    ).fetchone()
    if row is None or "collection_amount" in (row["sql"] or ""):
        return
    cols = ", ".join(_CORRECTIONS_COLUMNS)
    with conn:
        conn.execute("DROP TABLE IF EXISTS corrections_new")
        conn.execute(f"CREATE TABLE corrections_new ({_CORRECTIONS_BODY})")
        conn.execute(f"INSERT INTO corrections_new ({cols}) SELECT {cols} FROM corrections")
        conn.execute("DROP TABLE corrections")
        conn.execute("ALTER TABLE corrections_new RENAME TO corrections")


def ensure_db():
    """Initialise the SQLite DB, migrating existing CSVs on first run.

    Call once at application startup (collmenu.py). Safe to call multiple times.
    """
    fresh = not _db_path().exists()
    init_db()
    if fresh:
        _migrate_csv_to_db()


_V_FIELDS = ["bill_no", "date", "amount", "balance", "beat", "salesman", "created_by", "created_at"]
_I_FIELDS = ["bill_no", "date", "amount", "salesman", "created_by", "created_at"]
_U_FIELDS = ["name", "role", "password_hash"]
_B_FIELDS = ["name", "salesman"]
_P_FIELDS = ["role", "action_key"]
_CHK_FIELDS = ["bill_no", "beat", "salesman", "bank", "branch", "check_no",
               "check_date", "amount", "status", "recorded_by", "recorded_at",
               "resolved_by", "resolved_at", "resolution_note"]


def _migrate_csv_table(conn, table, path, fields):
    """Bulk-load one CSV file's rows into `table` via INSERT OR IGNORE (dedup on PK)."""
    if not path.exists():
        return
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cols = ", ".join(fields)
                placeholders = ", ".join("?" * len(fields))
                values = [row.get(fld, "") for fld in fields]
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})",
                    values,
                )
    except Exception:
        pass


def _migrate_csv_to_db():
    """Bulk-load existing CSV data into the newly created SQLite DB."""
    tables = [
        ("users",                  DATA_DIR / "users.csv",                  _U_FIELDS),
        ("beats",                  DATA_DIR / "beats.csv",                  _B_FIELDS),
        ("permissions",            DATA_DIR / "permissions.csv",            _P_FIELDS),
        ("vouchers",               DATA_DIR / "vouchers.csv",               _V_FIELDS),
        ("installments",           DATA_DIR / "installments.csv",           _I_FIELDS),
        ("completed_vouchers",     DATA_DIR / "completed_vouchers.csv",     _V_FIELDS),
        ("completed_installments", DATA_DIR / "completed_installments.csv", _I_FIELDS),
        ("checks",                 DATA_DIR / "checks.csv",                 _CHK_FIELDS),
    ]
    conn = get_db()
    try:
        for table, path, fields in tables:
            _migrate_csv_table(conn, table, path, fields)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def bill_no_sort_key(bill_no):
    return (0, int(bill_no)) if bill_no.isdigit() else (1, bill_no)


def ensure_staging_dir():
    STAGING_DIR.mkdir(parents=True, exist_ok=True)


def ensure_prints_dir():
    PRINTS_DIR.mkdir(parents=True, exist_ok=True)


def hash_password(password: str) -> str:
    """Return 'salt_hex:hash_hex' for PBKDF2-SHA256 password storage."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return binascii.hexlify(salt).decode() + ':' + binascii.hexlify(dk).decode()


def _verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt_hex, hash_hex = stored_hash.split(':')
        salt = binascii.unhexlify(salt_hex)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return binascii.hexlify(dk).decode() == hash_hex
    except Exception:
        return False


def verify_user(name: str, password: str):
    """Return User if credentials match a salesman/supervisor/distributor row, else None."""
    if not _db_path().exists():
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT role, password_hash, must_change_password FROM users WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    role = row["role"]
    stored = row["password_hash"]
    if role in ('salesman', 'supervisor', 'distributor') and _verify_password(stored, password):
        return User(name=name, role=role, must_change_password=bool(row["must_change_password"]))
    return None


def load_permissions():
    """Return dict[role -> frozenset[action_key]] from the permissions table."""
    if not _db_path().exists():
        raise FileNotFoundError(f"Database not found: {_db_path()}")
    conn = get_db()
    try:
        rows = conn.execute("SELECT role, action_key FROM permissions").fetchall()
    finally:
        conn.close()
    result = {}
    for row in rows:
        role = row["role"].strip()
        key = row["action_key"].strip()
        if role and key:
            result.setdefault(role, set()).add(key)
    return {r: frozenset(keys) for r, keys in result.items()}


# ---------------------------------------------------------------------------
# User / beat lifecycle management (create/edit/delete, password lifecycle)
#
# Web-only feature (distributor's Manage Users / Manage Beats screens plus
# self-service /profile password change). Business validation lives here
# rather than in coll_orchestrate.py because it has no stage-transition
# sequencing to keep in sync between CLI and web (this feature has no CLI
# counterpart), and — like apply_installment_correction/apply_voucher_amendment
# above — each guard must run inside the same transaction as its write to
# avoid a check-then-write race.
# ---------------------------------------------------------------------------

USER_ROLES = ("distributor", "supervisor", "salesman")  # 'system' excluded — not creatable/editable via UI
# Roles a distributor may hand out via /manage/users. Excludes 'distributor' itself —
# exactly one distributor may ever exist, created only by register_first_distributor().
ASSIGNABLE_ROLES = ("supervisor", "salesman")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_new_name(name, kind):
    name = (name or "").strip()
    if not name:
        raise ValueError(f"{kind} name is required")
    if not _NAME_RE.match(name):
        raise ValueError(f"{kind} name may only contain letters, digits, '.', '_', '-'")
    return name


def _validate_password(password, confirm=None):
    if confirm is not None and password != confirm:
        raise ValueError("password and confirmation do not match")
    if not password or len(password) < 6:
        raise ValueError("password must be at least 6 characters")


def has_any_users() -> bool:
    """True if the users table has at least one row (bootstrap has happened)."""
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    finally:
        conn.close()


SECRET_QUESTION_MIN = 5
SECRET_QUESTION_MAX = 200
SECRET_ANSWER_MIN = 3
SECRET_ANSWER_MAX = 100


def _normalize_secret_answer(answer):
    """Trim, casefold and collapse inner whitespace so "  Fluffy  Dog " and
    "fluffy dog" hash identically."""
    return " ".join((answer or "").split()).casefold()


def _validate_secret_question(question, answer):
    """Return (question, normalized_answer) or raise ValueError."""
    question = " ".join((question or "").split())
    if not (SECRET_QUESTION_MIN <= len(question) <= SECRET_QUESTION_MAX):
        raise ValueError(
            f"secret question must be {SECRET_QUESTION_MIN}-{SECRET_QUESTION_MAX} characters")
    normalized = _normalize_secret_answer(answer)
    if not (SECRET_ANSWER_MIN <= len(normalized) <= SECRET_ANSWER_MAX):
        raise ValueError(
            f"secret answer must be {SECRET_ANSWER_MIN}-{SECRET_ANSWER_MAX} characters")
    return question, normalized


def register_first_distributor(name, password, confirm_password,
                               secret_question=None, secret_answer=None):
    """Bootstrap-only: create the first distributor account when the users
    table is empty, so a fresh deployment with nobody able to log in can
    stand itself up without an existing distributor session.

    must_change_password=0 — the registrant already chose their own password,
    unlike create_user() where a distributor sets a placeholder for someone
    else. When a secret question/answer is supplied (the web form requires
    it) it is stored in the same insert, enabling "Forgot password?".
    Raises ValueError for invalid name/password/question, or if a user
    already exists (closes the race between the GET check and this submit).
    """
    name = _validate_new_name(name, "user")
    _validate_password(password, confirm_password)
    question = answer_hash = None
    if secret_question is not None or secret_answer is not None:
        question, normalized = _validate_secret_question(secret_question, secret_answer)
        answer_hash = hash_password(normalized)
    conn = get_db()
    try:
        with conn:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
                raise ValueError("registration is closed — an account already exists")
            conn.execute(
                "INSERT INTO users (name, role, password_hash, must_change_password,"
                " secret_question, secret_answer_hash)"
                " VALUES (?, 'distributor', ?, 0, ?, ?)",
                (name, hash_password(password), question, answer_hash),
            )
    finally:
        conn.close()


def get_distributor_secret_question():
    """The distributor's secret question, or None when there is no
    distributor or none has been set — drives whether "Forgot password?" is
    offered at all."""
    if not _db_path().exists():
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT secret_question FROM users WHERE role = 'distributor'"
            " AND secret_question IS NOT NULL AND secret_question <> '' AND"
            " secret_answer_hash IS NOT NULL AND secret_answer_hash <> ''"
            " LIMIT 1").fetchone()
    finally:
        conn.close()
    return row["secret_question"] if row else None


def get_secret_question_for(name):
    """The secret question for `name` only if that account is the distributor
    with a question set, else None (callers must not distinguish the reasons)."""
    if not _db_path().exists():
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT secret_question FROM users WHERE name = ? AND role = 'distributor'"
            " AND secret_question IS NOT NULL AND secret_question <> ''"
            " AND secret_answer_hash IS NOT NULL AND secret_answer_hash <> ''",
            (name,)).fetchone()
    finally:
        conn.close()
    return row["secret_question"] if row else None


def set_secret_question(name, current_password, question, answer):
    """Distributor sets/changes their secret question (Profile). Requires the
    current password so a walk-up on an unlocked session can't plant their
    own answer. Raises ValueError otherwise."""
    question, normalized = _validate_secret_question(question, answer)
    conn = get_db()
    try:
        with conn:
            row = conn.execute(
                "SELECT role, password_hash FROM users WHERE name = ?", (name,)).fetchone()
            if row is None or row["role"] != "distributor":
                raise ValueError("only the distributor can set a secret question")
            if not _verify_password(row["password_hash"], current_password):
                raise ValueError("current password is incorrect")
            conn.execute(
                "UPDATE users SET secret_question = ?, secret_answer_hash = ? WHERE name = ?",
                (question, hash_password(normalized), name))
    finally:
        conn.close()


RESET_WRONG_ANSWER = "the answer is incorrect"


def reset_password_with_secret_answer(name, answer, new_password, confirm_password):
    """Forgot-password reset for the distributor. Refuses unless `name` is the
    distributor with a question set. Wrong answer and ineligible account raise
    the SAME generic ValueError (RESET_WRONG_ANSWER) so callers can't tell
    them apart. Writes the new password with must_change_password=False — the
    user chose it themselves, exactly like /register."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT secret_answer_hash FROM users WHERE name = ? AND role = 'distributor'",
            (name,)).fetchone()
    finally:
        conn.close()
    stored = (row["secret_answer_hash"] or "") if row else ""
    if not stored or not _verify_password(stored, _normalize_secret_answer(answer)):
        raise ValueError(RESET_WRONG_ANSWER)
    _validate_password(new_password, confirm_password)
    set_user_password(name, hash_password(new_password), must_change_password=False)


def create_user(name, role, password, confirm_password):
    """Insert a new user with must_change_password=1 — creation is always
    paired with a forced first-login password change.

    Raises ValueError for an empty/invalid name, a role outside ASSIGNABLE_ROLES
    (this also rejects 'distributor' — exactly one may ever exist, created only
    via registration), a password/confirmation mismatch or too-short password,
    or a duplicate name.
    """
    name = _validate_new_name(name, "user")
    if role not in ASSIGNABLE_ROLES:
        raise ValueError(f"invalid role {role!r}")
    _validate_password(password, confirm_password)
    conn = get_db()
    try:
        with conn:
            try:
                conn.execute(
                    "INSERT INTO users (name, role, password_hash, must_change_password)"
                    " VALUES (?, ?, ?, 1)",
                    (name, role, hash_password(password)),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"user {name!r} already exists")
    finally:
        conn.close()


def load_user(name):
    """Return one user's admin-view dict (name, role, must_change_password) or None. Never password_hash."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT name, role, must_change_password FROM users WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = dict(row)
    d["must_change_password"] = bool(d["must_change_password"])
    return d


def load_users_admin():
    """Return every user's admin-view dict (name, role, must_change_password), ordered by name."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT name, role, must_change_password FROM users ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["must_change_password"] = bool(d["must_change_password"])
        result.append(d)
    return result


def update_user_role(name, role):
    """Change a user's role only (name is the immutable primary key).

    Raises ValueError for an unknown user, a role outside ASSIGNABLE_ROLES
    (this also rejects promoting anyone to 'distributor' — exactly one may
    ever exist), or when the target is the distributor account (its role is
    permanent, not just protected while it's the last one).
    """
    if role not in ASSIGNABLE_ROLES:
        raise ValueError(f"invalid role {role!r}")
    conn = get_db()
    try:
        with conn:
            row = conn.execute("SELECT role FROM users WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"user {name!r} not found")
            if row["role"] == "distributor":
                raise ValueError("the distributor account's role cannot be changed")
            conn.execute("UPDATE users SET role = ? WHERE name = ?", (role, name))
    finally:
        conn.close()


# Live-join reference checks only. Deliberately excludes corrections.requested_by /
# amendments.amended_by / amendment_requests.requested_by|resolved_by — those are
# frozen audit-trail text snapshots, not live joins, so a since-deleted user's
# name staying there is harmless and does not block deletion.
_USER_REFERENCE_QUERIES = [
    ("beats.salesman",                    "SELECT 1 FROM beats WHERE salesman = ? LIMIT 1"),
    ("vouchers.salesman",                 "SELECT 1 FROM vouchers WHERE salesman = ? LIMIT 1"),
    ("vouchers.created_by",               "SELECT 1 FROM vouchers WHERE created_by = ? LIMIT 1"),
    ("installments.salesman",             "SELECT 1 FROM installments WHERE salesman = ? LIMIT 1"),
    ("installments.created_by",           "SELECT 1 FROM installments WHERE created_by = ? LIMIT 1"),
    ("completed_vouchers.salesman",       "SELECT 1 FROM completed_vouchers WHERE salesman = ? LIMIT 1"),
    ("completed_vouchers.created_by",     "SELECT 1 FROM completed_vouchers WHERE created_by = ? LIMIT 1"),
    ("completed_installments.salesman",   "SELECT 1 FROM completed_installments WHERE salesman = ? LIMIT 1"),
    ("completed_installments.created_by", "SELECT 1 FROM completed_installments WHERE created_by = ? LIMIT 1"),
]


def user_is_referenced(conn, name):
    """True if `name` appears in any live master-data column (see
    _USER_REFERENCE_QUERIES). Takes an open connection so callers can check
    inside the same transaction as a delete, avoiding a check-then-write race."""
    return any(conn.execute(sql, (name,)).fetchone() for _, sql in _USER_REFERENCE_QUERIES)


def delete_user(name, current_user_name):
    """Hard delete. Raises ValueError if the user doesn't exist, is the
    caller's own account (self-lockout guard), is the distributor account
    (permanent — can never be deleted, not just protected while it's the
    last one), or is referenced by existing beats/vouchers/installments."""
    conn = get_db()
    try:
        with conn:
            row = conn.execute("SELECT role FROM users WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"user {name!r} not found")
            if name == current_user_name:
                raise ValueError("you cannot delete your own account")
            if row["role"] == "distributor":
                raise ValueError("the distributor account cannot be deleted")
            if user_is_referenced(conn, name):
                raise ValueError(
                    f"user {name!r} is referenced in existing vouchers/installments/beats"
                    " and cannot be deleted")
            conn.execute("DELETE FROM users WHERE name = ?", (name,))
    finally:
        conn.close()


def set_user_password(name, password_hash_value, must_change_password):
    """Low-level primitive: overwrite a user's password hash and forced-change flag."""
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = ? WHERE name = ?",
                (password_hash_value, 1 if must_change_password else 0, name),
            )
            if cur.rowcount == 0:
                raise ValueError(f"user {name!r} not found")
    finally:
        conn.close()


def reset_user_password(name, new_password, confirm_password):
    """Distributor-initiated reset. Always sets must_change_password=1 — the
    user must set their own password again at next login, same as a freshly
    created account."""
    _validate_password(new_password, confirm_password)
    set_user_password(name, hash_password(new_password), must_change_password=True)


def change_own_password(name, current_password, new_password, confirm_password):
    """Self-service password change (/profile). Verifies current_password,
    rejects new==current, and clears must_change_password on success — this
    is the mechanism that resolves the forced-first-login-change flow.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"user {name!r} not found")
    if not _verify_password(row["password_hash"], current_password):
        raise ValueError("current password is incorrect")
    if new_password != confirm_password:
        raise ValueError("new password and confirmation do not match")
    if new_password == current_password:
        raise ValueError("new password must be different from the current password")
    _validate_password(new_password)
    set_user_password(name, hash_password(new_password), must_change_password=False)


# ---------------------------------------------------------------------------
# Beat lifecycle management
# ---------------------------------------------------------------------------

def create_beat(name, salesman):
    """Raises ValueError for an empty/invalid name, an unknown/non-salesman
    assignee, or a duplicate beat name."""
    name = _validate_new_name(name, "beat")
    salesman = (salesman or "").strip()
    conn = get_db()
    try:
        with conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE name = ? AND role = 'salesman'", (salesman,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown salesman {salesman!r}")
            try:
                conn.execute(
                    "INSERT INTO beats (name, salesman) VALUES (?, ?)", (name, salesman)
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"beat {name!r} already exists")
    finally:
        conn.close()


def update_beat_salesman(name, salesman):
    """Reassign a beat's salesman only (name is the immutable primary key).

    Raises ValueError for an unknown beat or an unknown/non-salesman assignee.
    """
    salesman = (salesman or "").strip()
    conn = get_db()
    try:
        with conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE name = ? AND role = 'salesman'", (salesman,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown salesman {salesman!r}")
            cur = conn.execute(
                "UPDATE beats SET salesman = ? WHERE name = ?", (salesman, name)
            )
            if cur.rowcount == 0:
                raise ValueError(f"beat {name!r} not found")
    finally:
        conn.close()


_BEAT_REFERENCE_QUERIES = [
    ("vouchers.beat",           "SELECT 1 FROM vouchers WHERE beat = ? LIMIT 1"),
    ("completed_vouchers.beat", "SELECT 1 FROM completed_vouchers WHERE beat = ? LIMIT 1"),
]


def beat_is_referenced(conn, name):
    """True if `name` appears as a beat on any current or historical voucher."""
    return any(conn.execute(sql, (name,)).fetchone() for _, sql in _BEAT_REFERENCE_QUERIES)


def delete_beat(name):
    """Hard delete. Raises ValueError if the beat doesn't exist or has
    existing vouchers (active or historical) referencing it."""
    conn = get_db()
    try:
        with conn:
            row = conn.execute("SELECT 1 FROM beats WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"beat {name!r} not found")
            if beat_is_referenced(conn, name):
                raise ValueError(
                    f"beat {name!r} has existing vouchers (active or historical)"
                    " and cannot be deleted")
            conn.execute("DELETE FROM beats WHERE name = ?", (name,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Master data reads (SQLite)
# ---------------------------------------------------------------------------

def load_vouchers_raw():
    """Read all rows from the vouchers table as a list of dicts."""
    if not _db_path().exists():
        raise FileNotFoundError(f"Database not found: {_db_path()}")
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT bill_no, date, amount, balance, beat, salesman, created_by, created_at"
            " FROM vouchers"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def load_beats_raw():
    """Return list of beat dicts (name, salesman) from the beats table."""
    if not _db_path().exists():
        return []
    conn = get_db()
    try:
        rows = conn.execute("SELECT name, salesman FROM beats ORDER BY name").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def load_users_raw():
    """Return list of user dicts (name, role) from the users table."""
    if not _db_path().exists():
        return []
    conn = get_db()
    try:
        rows = conn.execute("SELECT name, role FROM users").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def load_installments_for_bill(bill_no, completed=False):
    """Return list of installment dicts for a bill_no from installments or
    completed_installments. Includes the surrogate `id` — the only unique row
    identity (duplicate bill_no/date/amount rows are legal) — so correction
    requests can target one specific installment."""
    if not _db_path().exists():
        return []
    table = "completed_installments" if completed else "installments"
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT id, bill_no, date, amount, salesman, created_by, created_at"
            f" FROM {table} WHERE bill_no = ?",
            (bill_no,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def load_completed_voucher(bill_no):
    """Return completed_vouchers row as dict, or None if not found."""
    if not _db_path().exists():
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT bill_no, date, amount, balance, beat, salesman, created_by, created_at"
            " FROM completed_vouchers WHERE bill_no = ?",
            (bill_no,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def load_all_existing_bill_nos():
    """Return set of all bill_nos from vouchers and completed_vouchers."""
    if not _db_path().exists():
        return set()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT bill_no FROM vouchers UNION SELECT bill_no FROM completed_vouchers"
        ).fetchall()
    finally:
        conn.close()
    return {r["bill_no"] for r in rows}


def load_vouchers_by_bill_nos(bill_nos):
    """Return dict[bill_no -> voucher row dict] for the given bill_nos.

    Queries the vouchers table in chunks of 500 to stay under SQLite's
    parameter limit. Missing bill_nos are simply absent from the result;
    returns {} when the DB file is missing, so callers report every
    voucher as not found.
    """
    unique = sorted({b for b in bill_nos if b})
    if not unique or not _db_path().exists():
        return {}
    result = {}
    conn = get_db()
    try:
        for i in range(0, len(unique), 500):
            chunk = unique[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT bill_no, date, amount, balance, beat, salesman"
                f" FROM vouchers WHERE bill_no IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                result[r["bill_no"]] = dict(r)
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Master data writes (SQLite)
# ---------------------------------------------------------------------------

def _parse_payment_strict(voucher):
    """Return the voucher's payment as a finite Decimal, or None when empty.

    Raises ValueError on unparseable/non-finite payments — post write paths
    must abort before touching the master tables, never skip silently.
    """
    payment = (voucher.get("payment") or "").strip()
    if not payment:
        return None
    try:
        amount = Decimal(payment)
        if not amount.is_finite():
            raise InvalidOperation
    except (ValueError, InvalidOperation):
        raise ValueError(
            f"invalid payment for {voucher.get('bill_no', '?')}: {payment!r}")
    return amount


def _append_installments(conn, vouchers, created_by="app"):
    """Insert one installment row per paying voucher on an open connection.

    created_by is the audit identity of the user performing the post —
    callers should pass the logged-in user's name. Raises ValueError on an
    unparseable payment so the enclosing transaction rolls back. Returns
    dict[bill_no -> new installment id] for the rows just inserted, so a
    caller (_append_checks) can link a check row to its installment.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    created_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    installment_ids = {}
    for v in vouchers:
        amount = _parse_payment_strict(v)
        if amount is None or amount <= 0:
            continue
        collection_date = (v.get("payment_date") or "").strip() or today
        payment_type = (v.get("payment_type") or "cash").strip() or "cash"
        payment_ref = ""
        if payment_type == "upi" and (v.get("upi_txn_id") or "").strip():
            payment_ref = json.dumps({"txn_id": v["upi_txn_id"].strip()})
        elif payment_type == "returns" and v.get("return_items"):
            payment_ref = json.dumps({"items": v["return_items"]})
        cur = conn.execute(
            "INSERT INTO installments"
            " (bill_no, date, amount, salesman, created_by, created_at,"
            " payment_type, payment_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (v["bill_no"], collection_date, str(amount), v["salesman"], created_by, created_at,
             payment_type, payment_ref),
        )
        installment_ids[v["bill_no"]] = cur.lastrowid
    return installment_ids


def _append_checks(conn, vouchers, installment_ids, recorded_by="app"):
    """Insert one 'pending' checks row per voucher paid by check, on an open
    connection. installment_ids is the dict _append_installments just
    returned, used only as an audit pointer (checks.installment_id is not
    FK-enforced — see the DDL comment). Raises ValueError on an unparseable
    payment so the enclosing transaction rolls back."""
    recorded_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for v in vouchers:
        if (v.get("payment_type") or "").strip() != "check":
            continue
        amount = _parse_payment_strict(v)
        if amount is None or amount <= 0:
            continue
        conn.execute(
            "INSERT INTO checks"
            " (installment_id, bill_no, beat, salesman, bank, branch, check_no,"
            " check_date, amount, status, recorded_by, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (installment_ids.get(v["bill_no"]), v["bill_no"],
             v.get("beat", ""), v.get("salesman", ""),
             (v.get("check_bank") or "").strip(), (v.get("check_branch") or "").strip(),
             (v.get("check_no") or "").strip(), (v.get("check_date") or "").strip(),
             str(amount), recorded_by, recorded_at),
        )


def _update_vouchers_balance(conn, vouchers):
    """Deduct payments from voucher balances on an open connection.

    Returns list of bill_nos that reached zero. Raises ValueError on an
    unparseable payment, a voucher missing from master, or a corrupt stored
    balance — the whole payment_map is parsed before the first UPDATE so a
    bad row aborts the enclosing transaction rather than half-applying.
    """
    payment_map = {}
    for v in vouchers:
        amount = _parse_payment_strict(v)
        if amount is not None:
            payment_map[v["bill_no"]] = amount
    if not payment_map:
        return []

    completed_bill_nos = []
    for bill_no, payment in payment_map.items():
        row = conn.execute(
            "SELECT balance FROM vouchers WHERE bill_no = ?", (bill_no,)
        ).fetchone()
        if row is None:
            raise ValueError(f"voucher {bill_no} not found in master — posting aborted")
        try:
            old_balance = Decimal(row["balance"])
            if not old_balance.is_finite():
                raise InvalidOperation
        except (ValueError, InvalidOperation):
            raise ValueError(
                f"voucher {bill_no} has an invalid stored balance {row['balance']!r}")
        new_balance = max(Decimal("0"), old_balance - payment)
        conn.execute(
            "UPDATE vouchers SET balance = ? WHERE bill_no = ?",
            (str(new_balance.quantize(Decimal("0.01"))), bill_no),
        )
        if new_balance == Decimal("0"):
            completed_bill_nos.append(bill_no)
    return completed_bill_nos


def _archive_completed(conn, bill_nos):
    """Move completed vouchers and their installments to completed_* tables
    on an open connection."""
    if not bill_nos:
        return
    bill_list = list(bill_nos)
    ph = ",".join("?" * len(bill_list))
    rows = conn.execute(
        f"SELECT bill_no, date, amount, balance, beat, salesman, created_by, created_at"
        f" FROM vouchers WHERE bill_no IN ({ph})",
        bill_list,
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO completed_vouchers"
            " (bill_no, date, amount, balance, beat, salesman, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (r["bill_no"], r["date"], r["amount"], r["balance"],
             r["beat"], r["salesman"], r["created_by"], r["created_at"]),
        )

    inst_rows = conn.execute(
        f"SELECT bill_no, date, amount, salesman, created_by, created_at,"
        f" payment_type, payment_ref"
        f" FROM installments WHERE bill_no IN ({ph})",
        bill_list,
    ).fetchall()
    for r in inst_rows:
        conn.execute(
            "INSERT INTO completed_installments"
            " (bill_no, date, amount, salesman, created_by, created_at,"
            " payment_type, payment_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (r["bill_no"], r["date"], r["amount"],
             r["salesman"], r["created_by"], r["created_at"],
             r["payment_type"], r["payment_ref"]),
        )
    # Installments reference vouchers via FK, so they must go first.
    conn.execute(f"DELETE FROM installments WHERE bill_no IN ({ph})", bill_list)
    conn.execute(f"DELETE FROM vouchers WHERE bill_no IN ({ph})", bill_list)


def apply_post_to_db(vouchers, created_by="app"):
    """All DB writes for posting one report in a single transaction:
    insert installments, deduct voucher balances, archive fully-settled
    vouchers. Returns the list of bill_nos that reached zero balance.
    Raises on any error, rolling back so no partial write persists.
    """
    if not _db_path().exists():
        raise FileNotFoundError(f"Database not found: {_db_path()}")
    conn = get_db()
    try:
        with conn:
            # Balances first: its existence check raises a clearer error for
            # a missing voucher than the installments FK would, and fully
            # settled vouchers are only archived (deleted) afterwards, so the
            # FK is satisfied when the installment rows are inserted.
            completed = _update_vouchers_balance(conn, vouchers)
            installment_ids = _append_installments(conn, vouchers, created_by)
            _append_checks(conn, vouchers, installment_ids, created_by)
            if completed:
                _archive_completed(conn, completed)
        return completed
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Correction requests
# ---------------------------------------------------------------------------

class CorrectionConflict(ValueError):
    """The correction's raise-time snapshot no longer matches current master
    data — the request is stale. Apply refuses; the request stays open for
    the distributor to reject (or the requester to withdraw and re-raise)."""


class AmendmentConflict(ValueError):
    """The amendment's load-time snapshot no longer matches current master
    data — someone else changed this voucher since the form was opened.
    Nothing commits; the caller reloads and re-renders with current data."""


def _correction_dict(row):
    d = dict(row)
    d["old"] = json.loads(d["old_json"]) if d["old_json"] else None
    d["new"] = json.loads(d["new_json"]) if d["new_json"] else None
    del d["old_json"], d["new_json"]
    return d


def insert_correction(record):
    """Insert a new open correction request; returns its id.

    `record` keys: kind, bill_no, requested_by, requested_at, and optionally
    report_stem, origin_stage, installment_id, old, new (dicts, stored as
    JSON), note. Validation of the requested values is the caller's job.
    """
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO corrections (kind, bill_no, report_stem, origin_stage,"
                " installment_id, old_json, new_json, note, status,"
                " requested_by, requested_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (record["kind"], record["bill_no"],
                 record.get("report_stem", ""), record.get("origin_stage", ""),
                 record.get("installment_id"),
                 json.dumps(record["old"]) if record.get("old") is not None else None,
                 json.dumps(record["new"]) if record.get("new") is not None else None,
                 record.get("note", ""),
                 record["requested_by"], record["requested_at"]))
            return cur.lastrowid
    finally:
        conn.close()


def load_correction(cid):
    """Return one correction request as a dict (old/new decoded), or None."""
    if not _db_path().exists():
        return None
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM corrections WHERE id = ?", (cid,)).fetchone()
    finally:
        conn.close()
    return _correction_dict(row) if row else None


def load_corrections(statuses=None, limit=None):
    """Return correction requests (most recent first), optionally filtered
    by an iterable of statuses and capped at `limit` rows."""
    if not _db_path().exists():
        return []
    sql = "SELECT * FROM corrections"
    params = []
    statuses = list(statuses or [])
    if statuses:
        sql += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
        params.extend(statuses)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_correction_dict(r) for r in rows]


def open_corrections_for_bills(bill_nos):
    """Return dict[bill_no -> list of open correction dicts] for the given bills."""
    unique = sorted({b for b in bill_nos if b})
    if not unique or not _db_path().exists():
        return {}
    conn = get_db()
    try:
        ph = ",".join("?" * len(unique))
        rows = conn.execute(
            f"SELECT * FROM corrections WHERE status = 'open' AND bill_no IN ({ph})"
            f" ORDER BY id",
            unique).fetchall()
    finally:
        conn.close()
    result = {}
    for r in rows:
        result.setdefault(r["bill_no"], []).append(_correction_dict(r))
    return result


def mark_correction_applied(cid, resolved_by, resolution_note="", now=None):
    """Flip an open request to 'applied' (one open-only UPDATE).

    For kinds whose change lives OUTSIDE this database (collection_amount
    edits the staged report JSON) — the master-data kinds flip status inside
    apply_installment_correction's transaction instead. Raises ValueError if
    the request is missing or no longer open. Returns the updated dict.
    """
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE corrections SET status = 'applied', resolved_by = ?,"
                " resolved_at = ?, resolution_note = ? WHERE id = ? AND status = 'open'",
                (resolved_by, now, resolution_note or "", cid))
            if cur.rowcount == 0:
                raise ValueError(f"correction {cid} is not open")
    finally:
        conn.close()
    return load_correction(cid)


def resolve_correction(cid, status, resolved_by, resolution_note="", now=None):
    """Stamp a reject/withdraw resolution on an open request (one UPDATE).

    Raises ValueError if the request is missing or no longer open. For
    'applied' use apply_installment_correction (master-data kinds) or
    mark_correction_applied (staged-data kinds).
    """
    if status not in ("rejected", "withdrawn"):
        raise ValueError(f"resolve_correction cannot set status {status!r}")
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE corrections SET status = ?, resolved_by = ?, resolved_at = ?,"
                " resolution_note = ? WHERE id = ? AND status = 'open'",
                (status, resolved_by, now, resolution_note or "", cid))
            if cur.rowcount == 0:
                raise ValueError(f"correction {cid} is not open")
    finally:
        conn.close()
    return load_correction(cid)


def _recompute_voucher_balance(conn, bill_no):
    """Recompute balance = voucher.amount − SUM(installments) from scratch on
    an open connection. Raises ValueError on a missing voucher or a negative
    result (the enclosing transaction rolls back). Returns the new Decimal."""
    vrow = conn.execute(
        "SELECT amount FROM vouchers WHERE bill_no = ?", (bill_no,)).fetchone()
    if vrow is None:
        raise ValueError(f"voucher {bill_no} not found in master")
    paid = Decimal("0")
    for r in conn.execute(
            "SELECT amount FROM installments WHERE bill_no = ?", (bill_no,)):
        paid += Decimal(r["amount"])
    new_balance = (Decimal(vrow["amount"]) - paid).quantize(Decimal("0.01"))
    if new_balance < 0:
        raise ValueError(
            f"correction would leave voucher {bill_no} with a negative balance"
            f" ({new_balance}) — refused")
    conn.execute("UPDATE vouchers SET balance = ? WHERE bill_no = ?",
                 (str(new_balance), bill_no))
    return new_balance


def apply_installment_correction(corr_id, resolved_by, resolution_note="", now=None):
    """Apply one open correction to master data and mark it applied — a
    single transaction, so any failure leaves both the master tables and the
    request status untouched.

    Steps: re-check the raise-time snapshot against the current row
    (CorrectionConflict on drift), perform the change for the request's kind,
    recompute the voucher balance from scratch (ValueError + rollback if it
    would go negative — zero is fine; the voucher stays active and completes
    naturally at the next post), then flip status to 'applied'.
    Returns the updated correction dict.
    """
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM corrections WHERE id = ?", (corr_id,)).fetchone()
            if row is None or row["status"] != "open":
                raise ValueError(f"correction {corr_id} is not open")
            corr = _correction_dict(row)
            kind, bill_no = corr["kind"], corr["bill_no"]
            old, new = corr["old"] or {}, corr["new"] or {}

            if kind in ("installment_amount", "installment_delete"):
                inst = conn.execute(
                    "SELECT date, amount FROM installments WHERE id = ? AND bill_no = ?",
                    (corr["installment_id"], bill_no)).fetchone()
                if (inst is None or inst["amount"] != old.get("amount")
                        or inst["date"] != old.get("date")):
                    raise CorrectionConflict(
                        f"correction {corr_id}: the installment no longer matches"
                        " the requested snapshot — master data changed since the"
                        " request was raised")
                if kind == "installment_amount":
                    conn.execute("UPDATE installments SET amount = ? WHERE id = ?",
                                 (new["amount"], corr["installment_id"]))
                else:
                    conn.execute("DELETE FROM installments WHERE id = ?",
                                 (corr["installment_id"],))
            elif kind == "installment_add":
                vrow = conn.execute(
                    "SELECT salesman FROM vouchers WHERE bill_no = ?",
                    (bill_no,)).fetchone()
                if vrow is None:
                    raise ValueError(f"voucher {bill_no} not found in master")
                conn.execute(
                    "INSERT INTO installments"
                    " (bill_no, date, amount, salesman, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (bill_no, new["date"], new["amount"], vrow["salesman"],
                     resolved_by, now))
            elif kind == "voucher_amount":
                vrow = conn.execute(
                    "SELECT amount FROM vouchers WHERE bill_no = ?",
                    (bill_no,)).fetchone()
                if vrow is None:
                    raise ValueError(f"voucher {bill_no} not found in master")
                if vrow["amount"] != old.get("amount"):
                    raise CorrectionConflict(
                        f"correction {corr_id}: the voucher amount no longer"
                        " matches the requested snapshot — master data changed"
                        " since the request was raised")
                conn.execute("UPDATE vouchers SET amount = ? WHERE bill_no = ?",
                             (new["amount"], bill_no))
            else:
                raise ValueError(f"correction {corr_id}: unknown kind {kind!r}")

            _recompute_voucher_balance(conn, bill_no)
            conn.execute(
                "UPDATE corrections SET status = 'applied', resolved_by = ?,"
                " resolved_at = ?, resolution_note = ? WHERE id = ?",
                (resolved_by, now, resolution_note or "", corr_id))
    finally:
        conn.close()
    return load_correction(corr_id)


# ---------------------------------------------------------------------------
# Voucher amendments
# ---------------------------------------------------------------------------

def _amendment_dict(row):
    d = dict(row)
    d["old"] = json.loads(d["old_json"]) if d["old_json"] else None
    d["new"] = json.loads(d["new_json"]) if d["new_json"] else None
    del d["old_json"], d["new_json"]
    return d


def load_amendment(aid):
    """Return one amendment as a dict (old/new decoded), or None."""
    if not _db_path().exists():
        return None
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM amendments WHERE id = ?", (aid,)).fetchone()
    finally:
        conn.close()
    return _amendment_dict(row) if row else None


def load_amendments(bill_no=None, limit=None):
    """Return amendments (most recent first), optionally filtered to one
    bill_no and capped at `limit` rows."""
    if not _db_path().exists():
        return []
    sql = "SELECT * FROM amendments"
    params = []
    if bill_no:
        sql += " WHERE bill_no = ?"
        params.append(bill_no)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_amendment_dict(r) for r in rows]


def _validate_amendment_amount(raw, label):
    """Strict finite positive Decimal, quantized 2dp, as a string — or raises
    ValueError with `label` for context. Last-gate backstop; the API
    pre-validates for friendly errors."""
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label}: invalid amount {raw!r}")
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{label}: invalid amount {raw!r}")
    return str(d.quantize(Decimal("0.01")))


def apply_voucher_amendment(bill_no, snapshot, new_state, amended_by, note="", now=None):
    """Apply a full-state edit of one voucher + its installments as ONE
    atomic transaction: re-check the load-time snapshot (AmendmentConflict on
    drift), validate every field, mutate, recompute the balance (ValueError +
    rollback if negative — `_recompute_voucher_balance` is the same function
    corrections use, so both paths share the one recompute policy), and write
    the audit row — all inside the same `with conn:` block, so any failure
    leaves master data and the audit trail untouched.

    snapshot / new_state shape: {"voucher": {date,amount,balance,beat,salesman},
    "installments": [{id,date,amount,salesman}, ...]}. Installment identity is
    by surrogate `id` throughout (duplicate rows are legal — coll_store.py
    load_installments_for_bill docstring): present in new_state -> UPDATE;
    id None -> INSERT (created_by=amended_by); present in snapshot but absent
    from new_state -> DELETE. Voucher balance is never taken from new_state —
    always recomputed. Returns the new amendment dict.
    """
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    try:
        with conn:
            vrow = conn.execute(
                "SELECT date, amount, balance, beat, salesman FROM vouchers"
                " WHERE bill_no = ?", (bill_no,)).fetchone()
            if vrow is None:
                raise ValueError(f"voucher {bill_no} not found in master")
            current_insts = {
                r["id"]: {"date": r["date"], "amount": r["amount"], "salesman": r["salesman"]}
                for r in conn.execute(
                    "SELECT id, date, amount, salesman FROM installments WHERE bill_no = ?",
                    (bill_no,))}

            snap_v = snapshot.get("voucher") or {}
            if dict(vrow) != {"date": snap_v.get("date"), "amount": snap_v.get("amount"),
                              "balance": snap_v.get("balance"), "beat": snap_v.get("beat"),
                              "salesman": snap_v.get("salesman")}:
                raise AmendmentConflict(
                    f"voucher {bill_no} changed since this amendment was loaded —"
                    " reload and try again")
            snap_insts = {
                i.get("id"): {"date": i.get("date"), "amount": i.get("amount"),
                              "salesman": i.get("salesman")}
                for i in snapshot.get("installments") or []}
            if current_insts != snap_insts:
                raise AmendmentConflict(
                    f"an installment on voucher {bill_no} changed since this"
                    " amendment was loaded — reload and try again")

            new_v = new_state.get("voucher") or {}
            new_date = (new_v.get("date") or "").strip()
            new_amount = _validate_amendment_amount(new_v.get("amount"), f"voucher {bill_no} amount")
            new_beat = (new_v.get("beat") or "").strip()
            new_salesman = (new_v.get("salesman") or "").strip()
            if not new_date:
                raise ValueError(f"voucher {bill_no}: date is required")
            if conn.execute("SELECT 1 FROM beats WHERE name = ?", (new_beat,)).fetchone() is None:
                raise ValueError(f"unknown beat {new_beat!r}")
            if conn.execute(
                    "SELECT 1 FROM users WHERE name = ? AND role = 'salesman'",
                    (new_salesman,)).fetchone() is None:
                raise ValueError(f"unknown salesman {new_salesman!r}")

            validated_rows = []
            for r in (new_state.get("installments") or []):
                rid = r.get("id")
                rdate = (r.get("date") or "").strip()
                ramount = _validate_amendment_amount(r.get("amount"), "installment amount")
                rsalesman = (r.get("salesman") or "").strip()
                if not rdate:
                    raise ValueError("installment date is required")
                if conn.execute(
                        "SELECT 1 FROM users WHERE name = ? AND role = 'salesman'",
                        (rsalesman,)).fetchone() is None:
                    raise ValueError(f"unknown salesman {rsalesman!r} on an installment")
                validated_rows.append((rid, rdate, ramount, rsalesman))

            kept_ids = {rid for rid, *_ in validated_rows if rid is not None}
            unknown_ids = kept_ids - set(current_insts)
            if unknown_ids:
                raise AmendmentConflict(
                    f"installment id(s) {sorted(unknown_ids)} on voucher {bill_no}"
                    " no longer exist — reload and try again")

            conn.execute(
                "UPDATE vouchers SET date = ?, amount = ?, beat = ?, salesman = ?"
                " WHERE bill_no = ?",
                (new_date, new_amount, new_beat, new_salesman, bill_no))

            removed_ids = set(current_insts) - kept_ids
            if removed_ids:
                ph = ",".join("?" * len(removed_ids))
                conn.execute(
                    f"DELETE FROM installments WHERE bill_no = ? AND id IN ({ph})",
                    (bill_no, *removed_ids))
            for rid, rdate, ramount, rsalesman in validated_rows:
                if rid is None:
                    conn.execute(
                        "INSERT INTO installments"
                        " (bill_no, date, amount, salesman, created_by, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (bill_no, rdate, ramount, rsalesman, amended_by, now))
                else:
                    conn.execute(
                        "UPDATE installments SET date = ?, amount = ?, salesman = ?"
                        " WHERE id = ? AND bill_no = ?",
                        (rdate, ramount, rsalesman, rid, bill_no))

            _recompute_voucher_balance(conn, bill_no)

            after_v = conn.execute(
                "SELECT date, amount, balance, beat, salesman FROM vouchers"
                " WHERE bill_no = ?", (bill_no,)).fetchone()
            after_insts = [dict(r) for r in conn.execute(
                "SELECT id, date, amount, salesman FROM installments WHERE bill_no = ?"
                " ORDER BY id", (bill_no,))]
            new_json = json.dumps({"voucher": dict(after_v), "installments": after_insts})
            old_json = json.dumps(snapshot)

            cur = conn.execute(
                "INSERT INTO amendments (bill_no, old_json, new_json, note,"
                " amended_by, amended_at) VALUES (?, ?, ?, ?, ?, ?)",
                (bill_no, old_json, new_json, note or "", amended_by, now))
            aid = cur.lastrowid
    finally:
        conn.close()
    return load_amendment(aid)


# ---------------------------------------------------------------------------
# Amendment requests
# ---------------------------------------------------------------------------

def insert_amendment_request(bill_no, note, requested_by, requested_at):
    """Insert a new open amendment request; returns its id.

    Free-text note only — validation of bill_no/eligibility is the caller's
    job (coll_orchestrate.raise_amendment_request).
    """
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO amendment_requests"
                " (bill_no, note, status, requested_by, requested_at)"
                " VALUES (?, ?, 'open', ?, ?)",
                (bill_no, note or "", requested_by, requested_at))
            return cur.lastrowid
    finally:
        conn.close()


def load_amendment_request(req_id):
    """Return one amendment request as a dict, or None."""
    if not _db_path().exists():
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM amendment_requests WHERE id = ?", (req_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def load_amendment_requests(statuses=None, limit=None):
    """Return amendment requests (most recent first), optionally filtered by
    an iterable of statuses and capped at `limit` rows."""
    if not _db_path().exists():
        return []
    sql = "SELECT * FROM amendment_requests"
    params = []
    statuses = list(statuses or [])
    if statuses:
        sql += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
        params.extend(statuses)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def open_amendment_requests_for_bills(bill_nos):
    """Return dict[bill_no -> list of open amendment-request dicts]."""
    unique = sorted({b for b in bill_nos if b})
    if not unique or not _db_path().exists():
        return {}
    conn = get_db()
    try:
        ph = ",".join("?" * len(unique))
        rows = conn.execute(
            f"SELECT * FROM amendment_requests WHERE status = 'open'"
            f" AND bill_no IN ({ph}) ORDER BY id",
            unique).fetchall()
    finally:
        conn.close()
    result = {}
    for r in rows:
        result.setdefault(r["bill_no"], []).append(dict(r))
    return result


def resolve_amendment_request(req_id, status, resolved_by, resolution_note="", now=None):
    """Stamp a reject/withdraw resolution on an open request (one UPDATE).

    Raises ValueError if the request is missing or no longer open. For
    'applied' use auto_resolve_amendment_requests, triggered by an actual
    voucher amendment landing on the bill.
    """
    if status not in ("rejected", "withdrawn"):
        raise ValueError(f"resolve_amendment_request cannot set status {status!r}")
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE amendment_requests SET status = ?, resolved_by = ?,"
                " resolved_at = ?, resolution_note = ? WHERE id = ? AND status = 'open'",
                (status, resolved_by, now, resolution_note or "", req_id))
            if cur.rowcount == 0:
                raise ValueError(f"amendment request {req_id} is not open")
    finally:
        conn.close()
    return load_amendment_request(req_id)


def auto_resolve_amendment_requests(bill_no, resolved_by, amendment_id, now=None):
    """Auto-close every open amendment request on `bill_no` as 'applied',
    linking them to the amendment that resolved them. Called as a side
    effect of apply_voucher_amendment landing on that bill — any open
    request is presumed addressed by the edit, regardless of whether its
    note matches the specific fields changed."""
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    try:
        with conn:
            conn.execute(
                "UPDATE amendment_requests SET status = 'applied', resolved_by = ?,"
                " resolved_at = ?, linked_amendment_id = ? WHERE bill_no = ? AND status = 'open'",
                (resolved_by, now, amendment_id, bill_no))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def load_check(check_id):
    """Return one check as a dict, or None."""
    if not _db_path().exists():
        return None
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def load_checks(statuses=None, limit=None):
    """Return checks (most recent first), optionally filtered by an iterable
    of statuses and capped at `limit` rows."""
    if not _db_path().exists():
        return []
    sql = "SELECT * FROM checks"
    params = []
    statuses = list(statuses or [])
    if statuses:
        sql += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
        params.extend(statuses)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def check_summary_counts(today=None, due_soon_days=2):
    """Return {'due_soon': n, 'overdue': n, 'bounced': n} for the menu banner
    and the Checks screen header.

    due_soon / overdue are not stored states — derived here from a pending
    check's check_date against `today` (default: real today), exactly like
    every other "open thing blocks/flags something" query in this app is
    computed at render time rather than persisted. bounced is an all-time
    count: bounced is terminal and there is no dismiss/acknowledge action.
    """
    if not _db_path().exists():
        return {"due_soon": 0, "overdue": 0, "bounced": 0}
    if today is None:
        today = datetime.now().date()
    elif isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d").date()
    today_iso = today.isoformat()
    due_by_iso = (today + timedelta(days=due_soon_days)).isoformat()
    conn = get_db()
    try:
        due_soon = conn.execute(
            "SELECT COUNT(*) FROM checks WHERE status = 'pending'"
            " AND check_date >= ? AND check_date <= ?",
            (today_iso, due_by_iso)).fetchone()[0]
        overdue = conn.execute(
            "SELECT COUNT(*) FROM checks WHERE status = 'pending' AND check_date < ?",
            (today_iso,)).fetchone()[0]
        bounced = conn.execute(
            "SELECT COUNT(*) FROM checks WHERE status = 'bounced'").fetchone()[0]
    finally:
        conn.close()
    return {"due_soon": due_soon, "overdue": overdue, "bounced": bounced}


def mark_check_encashed(check_id, resolved_by, resolution_note="", now=None):
    """Flip a pending check to 'encashed' (one open-only UPDATE) — the money
    was confirmed received; no further action. Raises ValueError if the
    check is missing or not pending. Returns the updated dict."""
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE checks SET status = 'encashed', resolved_by = ?,"
                " resolved_at = ?, resolution_note = ? WHERE id = ? AND status = 'pending'",
                (resolved_by, now, resolution_note or "", check_id))
            if cur.rowcount == 0:
                raise ValueError(f"check {check_id} is not pending")
    finally:
        conn.close()
    return load_check(check_id)


def mark_check_bounced(check_id, resolved_by, resolution_note="", now=None):
    """Bounce a pending check: delete its underlying installment (the money
    was never actually received) and recompute the voucher balance from
    scratch via _recompute_voucher_balance — the same invariant-preserving
    path an installment_delete correction uses (balance = amount minus the
    SUM of what's left in installments) — then flip status to 'bounced'.
    One transaction.

    If the voucher has already been archived (fully settled and moved to
    completed_vouchers/completed_installments by this very check), its
    installment row is gone and cannot be un-archived here — that's out of
    scope for this iteration. The check is still marked bounced, with a
    note explaining why the balance wasn't touched; the distributor can use
    Amend Voucher for a manual fix if that edge case needs one.

    Raises ValueError if the check is missing or not pending.
    """
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db()
    try:
        with conn:
            row = conn.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise ValueError(f"check {check_id} is not pending")
            note = resolution_note or ""
            inst = None
            if row["installment_id"] is not None:
                inst = conn.execute(
                    "SELECT id FROM installments WHERE id = ? AND bill_no = ?",
                    (row["installment_id"], row["bill_no"])).fetchone()
            if inst is not None:
                conn.execute("DELETE FROM installments WHERE id = ?", (inst["id"],))
                _recompute_voucher_balance(conn, row["bill_no"])
            else:
                note = (note + " " if note else "") + (
                    "(voucher already archived — balance not auto-adjusted)")
            conn.execute(
                "UPDATE checks SET status = 'bounced', resolved_by = ?,"
                " resolved_at = ?, resolution_note = ? WHERE id = ?",
                (resolved_by, now, note, check_id))
    finally:
        conn.close()
    return load_check(check_id)


def write_new_vouchers(vouchers):
    """Insert new vouchers into the vouchers table.

    INSERT OR IGNORE also silently skips rows violating the schema CHECK/FK
    constraints — callers must pre-validate via coll_data.validate_addv_batch
    / validate_single_voucher."""
    conn = get_db()
    try:
        with conn:
            for v in vouchers:
                conn.execute(
                    "INSERT OR IGNORE INTO vouchers"
                    " (bill_no, date, amount, balance, beat, salesman, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (v.get("bill_no", ""), v.get("date", ""), v.get("amount", ""),
                     v.get("balance", ""), v.get("beat", ""), v.get("salesman", ""),
                     v.get("created_by", ""), v.get("created_at", "")),
                )
    finally:
        conn.close()


def write_new_installments(installments):
    """Insert new installments into the installments table.

    INSERT OR IGNORE also silently skips rows violating the schema CHECK/FK
    constraints — callers must pre-validate via coll_data.validate_addv_batch."""
    if not installments:
        return
    conn = get_db()
    try:
        with conn:
            for inst in installments:
                conn.execute(
                    "INSERT OR IGNORE INTO installments"
                    " (bill_no, date, amount, salesman, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (inst.get("bill_no", ""), inst.get("date", ""), inst.get("amount", ""),
                     inst.get("salesman", ""), inst.get("created_by", ""), inst.get("created_at", "")),
                )
    finally:
        conn.close()


def reset_test_data_tables():
    """Delete all rows from the transactional tables (vouchers, installments,
    completed_vouchers, completed_installments). Leaves users/beats/permissions
    untouched. Used by scripts/generate_test_data.py to reproduce its
    full-reset-and-regenerate semantics against SQLite.
    """
    conn = get_db()
    try:
        with conn:
            for table in ("vouchers", "installments", "completed_vouchers", "completed_installments"):
                conn.execute(f"DELETE FROM {table}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Staging JSON helpers (unchanged — staging stays on disk as JSON)
# ---------------------------------------------------------------------------

def _load_pending_start_reports():
    """Reports generated but not yet supervisor-confirmed (stages.start == 'new')."""
    if not STAGING_DIR.exists():
        return []
    result = []
    for path in sorted(STAGING_DIR.glob('coll*.json')):
        try:
            with path.open(encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get('stages', {}).get('start') == 'new':
            result.append((path, data))
    return result


def _load_pending_submit_reports():
    """Reports submitted but not yet supervisor-confirmed (stages.submit == 'submitted')."""
    if not STAGING_DIR.exists():
        return []
    result = []
    for path in sorted(STAGING_DIR.glob('coll*.json')):
        try:
            with path.open(encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get('stages', {}).get('submit') != 'submitted':
            continue
        result.append((path, data))
    return result


def save_collection_json(path, vouchers):
    with path.open("w", encoding="utf-8") as f:
        json.dump(vouchers, f, indent=2)


def save_report_json(path, report_data):
    """Write a full report dict (stage/status/vouchers/...) to a JSON file."""
    with path.open("w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)


def load_collection_json(path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get("vouchers", [])


def load_report_json(path):
    """Read a full report dict (stages/selection/vouchers/...) from a JSON file."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_collection_text(path, beats, salesmen, vouchers, stage=None, status=None):
    if not vouchers:
        raise ValueError("No vouchers available to write to text report.")

    date_str = datetime.now().strftime("%Y-%m-%d")
    bill_width = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    vdate_width = max(len("voucher_date"), max(len(v.get("voucher_date", "")) for v in vouchers))
    balance_width = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    payment_width = max(len("collection"), max(len(v.get("payment", "")) for v in vouchers))

    header = (
        f"{ 'bill_no':<{bill_width}}  "
        f"{ 'voucher_date':<{vdate_width}}  "
        f"{ 'balance':>{balance_width}}  "
        f"{ 'collection':>{payment_width}}"
    )
    separator = "-" * len(header)

    lines = ["COLLECTION LIST"]
    lines += [
        f"Beats: {', '.join(beats)}",
        f"Salesmen: {', '.join(salesmen)}",
        f"Collection date: {date_str}",
        header,
        separator,
    ]

    for voucher in sorted(vouchers, key=lambda v: bill_no_sort_key(v["bill_no"])):
        lines.append(
            f"{voucher['bill_no']:<{bill_width}}  "
            f"{voucher.get('voucher_date', ''):<{vdate_width}}  "
            f"{voucher['balance']:>{balance_width}}  "
            f"{voucher.get('payment', ''):>{payment_width}}"
        )

    lines.append(separator)
    total_vouchers = len(vouchers)
    total_balance = sum(parse_decimal(v.get("balance")) for v in vouchers)
    total_payments = sum(parse_decimal(v.get("payment")) for v in vouchers)
    lines.append(f"Total vouchers: {total_vouchers}")
    lines.append(f"Sum of balances: {total_balance}")
    if total_payments > 0:
        lines.append(f"Total payments entered: {total_payments}")

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def sanitize_filename_component(value):
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())
    return safe or "unknown"


def acquire_beat_lock(beat_name):
    """Atomically claim a beat. Returns True if acquired, False if already locked."""
    ensure_staging_dir()
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", beat_name.strip()) or "unknown"
    path = STAGING_DIR / f".beatlock-{safe}.lock"
    try:
        path.open('x').close()
        return True
    except FileExistsError:
        return False


def release_beat_lock(beat_name):
    """Release a previously acquired beat lock."""
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", beat_name.strip()) or "unknown"
    path = STAGING_DIR / f".beatlock-{safe}.lock"
    path.unlink(missing_ok=True)


def _post_claim_path(report_path):
    return report_path.parent / f".posting-{report_path.stem}.lock"


def acquire_post_claim(report_path):
    """Atomically claim a report for posting. Returns True if acquired, False if
    another session is already posting it."""
    try:
        _post_claim_path(report_path).open('x').close()
        return True
    except FileExistsError:
        return False


def release_post_claim(report_path):
    """Release a previously acquired posting claim."""
    _post_claim_path(report_path).unlink(missing_ok=True)


def cancel_staging_report(report_path, beat_name):
    """Delete all staging files for a report and release its beat lock."""
    report_path.unlink(missing_ok=True)
    report_path.with_suffix(".txt").unlink(missing_ok=True)
    _installments_path(report_path).unlink(missing_ok=True)
    if beat_name:
        release_beat_lock(beat_name)


def _checkpoint_path():
    return STAGING_DIR / ".finalize_checkpoint.json"


def write_finalize_checkpoint(report_path, step):
    with _checkpoint_path().open("w", encoding="utf-8") as f:
        json.dump({"report": str(report_path), "step": step}, f)


def clear_finalize_checkpoint():
    _checkpoint_path().unlink(missing_ok=True)


def read_finalize_checkpoint():
    p = _checkpoint_path()
    if not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_staging_reports():
    if not STAGING_DIR.exists():
        return []
    return sorted(STAGING_DIR.glob("coll*.json"))


def _installments_path(report_path):
    return report_path.parent / f"{report_path.stem}-installments.json"


_PAYMENT_TYPE_SIDECAR_KEYS = (
    "payment_type", "upi_txn_id", "check_bank", "check_branch", "check_no", "check_date",
    "return_items")


def _save_installments(report_path, vouchers, bookmark_bill_no=None):
    data = {}
    for v in vouchers:
        if not v.get("payment"):
            continue
        entry = {"payment": v["payment"], "date": v.get("payment_date", "")}
        for key in _PAYMENT_TYPE_SIDECAR_KEYS:
            if v.get(key):
                entry[key] = v[key]
        data[v["bill_no"]] = entry
    if bookmark_bill_no:
        data["__bookmark__"] = bookmark_bill_no
    with _installments_path(report_path).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _load_installments(report_path):
    path = _installments_path(report_path)
    if not path.exists():
        return {}, None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        bookmark = data.pop("__bookmark__", None)
        data.pop("__status__", None)
        data = {
            bn: (entry if isinstance(entry, dict) else {"payment": entry, "date": ""})
            for bn, entry in data.items()
        }
        return data, bookmark
    except Exception:
        return {}, None


def _unique_archive_dest(name):
    dest = ARCHIVE_DIR / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while True:
        dest = ARCHIVE_DIR / f"{stem}_dup{counter}{suffix}"
        if not dest.exists():
            return dest
        counter += 1


def archive_files(paths):
    """Move each existing path in `paths` into ARCHIVE_DIR.

    Uses a '_dupN' suffix on name collisions instead of overwriting (Windows
    Path.rename() raises FileExistsError if the destination already exists).
    """
    ARCHIVE_DIR.mkdir(exist_ok=True)
    archived = {}
    for src in paths:
        if not src.exists():
            continue
        dest = _unique_archive_dest(src.name)
        src.rename(dest)
        archived[src] = dest
    return archived


# ---------------------------------------------------------------------------
# Print / HTML generation (unchanged)
# ---------------------------------------------------------------------------

def _build_print_column(report_data, bal_width, coll_width):
    W = _PRINT_COL_WIDTH
    sel_type = report_data.get("selection_type", "beat")
    sel = report_data.get("selection", [])

    if sel_type == "beat_salesman" and len(sel) >= 2:
        sal_width = max(1, W - len(sel[0]) - 2)
        heading = f"{sel[0]}  {sel[1]:>{sal_width}}"
    elif sel_type == "beat":
        heading = ",".join(sel)
    else:
        heading = ",".join(sel)

    dash = "-" * W
    col_hdr = f"{'Bill No':<7} {'Balance':>{bal_width}} {'Coll':>{coll_width}}"

    vouchers = report_data.get("vouchers", [])
    total_vouchers = len(vouchers)
    total_bal = sum(parse_decimal(v.get("balance")) for v in vouchers)
    summary = f"#:{total_vouchers}  Bal:{total_bal}"

    lines = [
        heading[:W].ljust(W),
        col_hdr[:W].ljust(W),
        dash,
    ]
    for v in sorted(vouchers, key=lambda v: bill_no_sort_key(v["bill_no"])):
        bill = v["bill_no"][-7:]
        bal = v.get("balance", "")
        pay = v.get("payment", "") or ""
        row = f"{bill:<7} {bal:>{bal_width}} {pay:>{coll_width}}"
        lines.append(row[:W].ljust(W))
    lines.append(summary[:W].ljust(W))
    return lines


def write_print_collection_txt(output_path, reports_data):
    if not reports_data:
        return

    all_vouchers = [v for r in reports_data for v in r.get("vouchers", [])]
    bal_width = max(
        len("Balance"),
        max((len(v.get("balance", "")) for v in all_vouchers), default=0),
    )
    coll_width = max(4, _PRINT_COL_WIDTH - 7 - 1 - bal_width - 1)

    columns = [_build_print_column(r, bal_width, coll_width) for r in reports_data]
    num_cols = len(columns)
    max_rows = max(len(c) for c in columns)

    for col in columns:
        while len(col) < max_rows:
            col.append(" " * _PRINT_COL_WIDTH)

    date_str = datetime.now().strftime("%Y-%m-%d")
    full_width = _PRINT_COL_WIDTH * num_cols + len(_PRINT_COL_SEP) * (num_cols - 1)
    eq_sep = "=" * full_width
    title_line = f"{'COLLECTION LIST':<{full_width - 10}}{date_str:>10}"

    output_lines = [eq_sep, title_line, eq_sep]
    remaining = _PRINT_PAGE_HEIGHT - _PRINT_FILE_HEADER_LINES

    for row_idx in range(max_rows):
        if remaining <= 0:
            output_lines.append("\f")
            remaining = _PRINT_PAGE_HEIGHT
        output_lines.append(_PRINT_COL_SEP.join(col[row_idx] for col in columns))
        remaining -= 1

    with output_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(output_lines) + "\n")


def _build_html_column(report_data):
    sel_type = report_data.get("selection_type", "beat")
    sel = report_data.get("selection", [])
    if sel_type == "beat_salesman" and len(sel) >= 2:
        heading = f"{sel[0]} / {sel[1]}"
    else:
        heading = ", ".join(sel)

    vouchers = report_data.get("vouchers", [])
    total_vouchers = len(vouchers)
    total_bal = sum(parse_decimal(v.get("balance")) for v in vouchers)
    total_coll = sum(parse_decimal(v.get("payment")) for v in vouchers)

    rows_html = "".join(
        f"<tr><td>{v['bill_no'][-7:]}</td>"
        f'<td class="sep">--</td>'
        f'<td class="num">{v.get("balance", "")}</td>'
        f'<td class="num">{v.get("payment", "") or ""}</td></tr>\n'
        for v in sorted(vouchers, key=lambda v: bill_no_sort_key(v["bill_no"]))
    )
    coll_str = str(total_coll) if total_coll > 0 else ""

    return (
        f'<div class="col-heading">{heading}</div>\n'
        f"<table>\n"
        f"<colgroup><col style=\"width:7ch\"><col style=\"width:2ch\"><col style=\"width:8ch\"><col></colgroup>\n"
        f"<thead><tr>"
        f"<td>Bill No</td>"
        f'<td class="sep"></td>'
        f'<td class="num">Balance</td>'
        f'<td class="num">Coll</td>'
        f"</tr></thead>\n"
        f"<tbody>\n{rows_html}</tbody>\n"
        f"<tfoot><tr>"
        f"<td colspan=\"3\">#{total_vouchers}&nbsp; Bal:{total_bal}</td>"
        f'<td class="num">{coll_str}</td>'
        f"</tr></tfoot>\n"
        f"</table>"
    )


def build_print_collection_html(reports_data, auto_print=False):
    """Return the printable collection-list document as an HTML string.

    auto_print embeds a window.print() call for web delivery; saved files
    (CLI path) must never fire the print dialog on open.
    """
    if not reports_data:
        return ""

    auto_print_script = "<script>window.print();</script>\n" if auto_print else ""
    date_str = datetime.now().strftime("%Y-%m-%d")
    col_divs = "\n".join(
        f'<div class="col">\n{_build_html_column(r)}\n</div>'
        for r in reports_data
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Collection List {date_str}</title>
<style>
  @page {{ margin: 10mm 8mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Courier New', Courier, monospace;
    font-size: 11pt;
    line-height: 1.05;
  }}
  .page-header {{
    display: flex;
    justify-content: space-between;
    font-weight: bold;
    font-size: 12.5pt;
    border-bottom: 2px solid #000;
    padding-bottom: 2px;
    margin-bottom: 4px;
  }}
  .columns {{
    display: flex;
    gap: 6px;
    align-items: flex-start;
  }}
  .col {{
    flex: 1;
    border-left: 1px solid #999;
    padding-left: 4px;
  }}
  .col:first-child {{
    border-left: none;
    padding-left: 0;
  }}
  .col-heading {{
    font-weight: bold;
    font-size: 10.5pt;
    white-space: nowrap;
    overflow: hidden;
    border-bottom: 1px solid #000;
    padding-bottom: 1px;
    margin-bottom: 1px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
  }}
  thead td {{
    font-weight: bold;
    border-bottom: 1px solid #000;
    padding: 0;
  }}
  tbody td {{
    padding: 1pt 0;
    white-space: nowrap;
  }}
  tfoot td {{
    font-weight: bold;
    border-top: 1px solid #000;
    padding: 0;
  }}
  .num {{ text-align: right; }}
  .sep {{ text-align: center; }}
</style>
</head>
<body>
<div class="page-header">
  <span>COLLECTION LIST</span>
  <span>{date_str}</span>
</div>
<div class="columns">
{col_divs}
</div>
{auto_print_script}</body>
</html>"""

    return html


def write_print_collection_html(output_path, reports_data):
    if not reports_data:
        return
    with output_path.open("w", encoding="utf-8") as f:
        f.write(build_print_collection_html(reports_data))


# ---------------------------------------------------------------------------
# Add-vouchers pipeline helpers (staging JSON — unchanged)
# ---------------------------------------------------------------------------

def read_csv_file(path):
    """Read a CSV file and return (fieldnames, rows). Used for batch import feature."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with p.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        if not reader.fieldnames:
            raise ValueError(f"File appears empty or has no header: {path}")
        return list(reader.fieldnames), rows


def load_addv_staged_bill_nos():
    """Return set of bill_nos in all non-finalized addv staging files."""
    bill_nos = set()
    if not STAGING_DIR.exists():
        return bill_nos
    for path in STAGING_DIR.glob("addv*.json"):
        try:
            with path.open(encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            if data.get("stages", {}).get("post") == "confirmed":
                continue
            for v in data.get("vouchers", []):
                b = v.get("bill_no", "").strip()
                if b:
                    bill_nos.add(b)
        except Exception:
            continue
    return bill_nos


def load_addv_pending_confirm():
    """Return (path, data) pairs for addv reports awaiting confirmation."""
    if not STAGING_DIR.exists():
        return []
    result = []
    for path in sorted(STAGING_DIR.glob("addv*.json")):
        try:
            with path.open(encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        stages = data.get("stages", {})
        if stages.get("add") == "done" and stages.get("confirm") != "confirmed":
            result.append((path, data))
    return result


def load_addv_pending_finalize():
    """Return (path, data) pairs for addv reports confirmed but not yet finalized."""
    if not STAGING_DIR.exists():
        return []
    result = []
    for path in sorted(STAGING_DIR.glob("addv*.json")):
        try:
            with path.open(encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        stages = data.get("stages", {})
        if stages.get("confirm") == "confirmed" and stages.get("post") != "confirmed":
            result.append((path, data))
    return result


def load_addv_batches():
    """Return (path, data) pairs for every non-archived addv batch, regardless
    of stage — used by the web-only onboarding hub, which derives its own
    status (pending_review/awaiting_resolution/ready_to_post) instead of
    relying on the old stages.confirm/post flags the CLI still uses."""
    if not STAGING_DIR.exists():
        return []
    result = []
    for path in sorted(STAGING_DIR.glob("addv*.json")):
        try:
            with path.open(encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        result.append((path, data))
    return result


def delete_staged_report(path):
    """Delete a staging JSON file outright (e.g. rejecting a bad addv batch)."""
    path.unlink(missing_ok=True)
