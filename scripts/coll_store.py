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
import sqlite3
from collections import namedtuple
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

User = namedtuple('User', ['name', 'role'])

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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name          TEXT PRIMARY KEY,
    role          TEXT NOT NULL,
    password_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS beats (
    name     TEXT PRIMARY KEY,
    salesman TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS permissions (
    role       TEXT NOT NULL,
    action_key TEXT NOT NULL,
    PRIMARY KEY (role, action_key)
);
CREATE TABLE IF NOT EXISTS vouchers (
    bill_no    TEXT PRIMARY KEY,
    date       TEXT NOT NULL,
    amount     TEXT NOT NULL,
    balance    TEXT NOT NULL,
    beat       TEXT NOT NULL,
    salesman   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS installments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no    TEXT NOT NULL,
    date       TEXT NOT NULL,
    amount     TEXT NOT NULL,
    salesman   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS completed_vouchers (
    bill_no    TEXT PRIMARY KEY,
    date       TEXT NOT NULL,
    amount     TEXT NOT NULL,
    balance    TEXT NOT NULL,
    beat       TEXT NOT NULL,
    salesman   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS completed_installments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no    TEXT NOT NULL,
    date       TEXT NOT NULL,
    amount     TEXT NOT NULL,
    salesman   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
"""


def init_db():
    """Create all tables in the DB (idempotent). Creates the DB file if absent.

    Also applies one-time additive migrations for DBs created before a given
    column/table existed (beats.salesman, permissions), backfilling from the
    matching CSV so an older collmgm.db catches up the moment it's reopened
    by newer code.
    """
    conn = get_db()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _backfill_beats_salesman(conn)
        _backfill_permissions(conn)
        conn.commit()
    finally:
        conn.close()


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


def _backfill_permissions(conn):
    """One-time seed of the permissions table from data/permissions.csv, if empty."""
    count = conn.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
    if count:
        return
    _migrate_csv_table(conn, "permissions", DATA_DIR / "permissions.csv", _P_FIELDS)


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
            "SELECT role, password_hash FROM users WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    role = row["role"]
    stored = row["password_hash"]
    if role in ('salesman', 'supervisor', 'distributor') and _verify_password(stored, password):
        return User(name=name, role=role)
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
    """Return list of installment dicts for a bill_no from installments or completed_installments."""
    if not _db_path().exists():
        return []
    table = "completed_installments" if completed else "installments"
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT bill_no, date, amount, salesman, created_by, created_at"
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
    unparseable payment so the enclosing transaction rolls back.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    created_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for v in vouchers:
        amount = _parse_payment_strict(v)
        if amount is None or amount <= 0:
            continue
        collection_date = (v.get("payment_date") or "").strip() or today
        conn.execute(
            "INSERT INTO installments"
            " (bill_no, date, amount, salesman, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (v["bill_no"], collection_date, str(amount), v["salesman"], created_by, created_at),
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
    conn.execute(f"DELETE FROM vouchers WHERE bill_no IN ({ph})", bill_list)

    inst_rows = conn.execute(
        f"SELECT bill_no, date, amount, salesman, created_by, created_at"
        f" FROM installments WHERE bill_no IN ({ph})",
        bill_list,
    ).fetchall()
    for r in inst_rows:
        conn.execute(
            "INSERT INTO completed_installments"
            " (bill_no, date, amount, salesman, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (r["bill_no"], r["date"], r["amount"],
             r["salesman"], r["created_by"], r["created_at"]),
        )
    conn.execute(f"DELETE FROM installments WHERE bill_no IN ({ph})", bill_list)


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
            _append_installments(conn, vouchers, created_by)
            completed = _update_vouchers_balance(conn, vouchers)
            if completed:
                _archive_completed(conn, completed)
        return completed
    finally:
        conn.close()


def write_new_vouchers(vouchers):
    """Insert new vouchers into the vouchers table."""
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
    """Insert new installments into the installments table."""
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


def _save_installments(report_path, vouchers, bookmark_bill_no=None):
    data = {
        v["bill_no"]: {"payment": v["payment"], "date": v.get("payment_date", "")}
        for v in vouchers if v.get("payment")
    }
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


def write_print_collection_html(output_path, reports_data):
    if not reports_data:
        return

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
</body>
</html>"""

    with output_path.open("w", encoding="utf-8") as f:
        f.write(html)


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
