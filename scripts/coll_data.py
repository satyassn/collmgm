"""
Data loading and query layer for collection management.

Reads master CSVs and staging JSON files. No print() or input() calls.
Imports only from coll_store. current_user parameter is reserved for future RBAC.
"""

import csv
import json
from datetime import datetime
from decimal import Decimal

from coll_store import (
    DATA_DIR, STAGING_DIR, load_vouchers_raw,
    _load_pending_start_reports, _load_pending_submit_reports,
)

NUMOF_TOP_AGED_VOUCHERS = 10
NUMOF_TOP_AMOUNT_VOUCHERS = 10


def load_beats(current_user=None):
    """Return list of beat names from beats.csv.

    For salesman role, returns only beats assigned to that salesman in beats.csv.
    """
    beats_file = DATA_DIR / "beats.csv"
    if not beats_file.exists():
        raise FileNotFoundError(f"Missing beats file: {beats_file}")

    beats = []
    with beats_file.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("name", "").strip()
            if not name:
                continue
            if current_user and current_user.role == 'salesman':
                if row.get("salesman", "").strip() != current_user.name:
                    continue
            beats.append(name)

    if not beats:
        raise ValueError("No beats found in data/beats.csv.")
    return beats


def load_salesmen(current_user=None):
    """Return list of salesman names from users.csv.

    For salesman role, returns only the current user's own name.
    """
    if current_user and current_user.role == 'salesman':
        return [current_user.name]

    salesmen = []
    users_file = DATA_DIR / "users.csv"
    if not users_file.exists():
        raise FileNotFoundError(f"Missing users file: {users_file}")

    with users_file.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            role = row.get("role", "").strip().lower()
            name = row.get("name", "").strip()
            if role == "salesman" and name:
                salesmen.append(name)

    if not salesmen:
        raise ValueError("No salesmen found in data/users.csv.")
    return salesmen


def load_beats_pending_summary(current_user=None):
    """Return dict[beat -> {"total": int, "balance_sum": Decimal, "by_salesman": dict}] for pending vouchers."""
    rows = load_vouchers_raw()
    summary = {}
    for row in rows:
        beat = row.get("beat", "").strip()
        if not beat:
            continue
        try:
            bal = Decimal(row.get("balance", "0").strip())
        except Exception:
            continue
        if bal <= 0:
            continue
        salesman = row.get("salesman", "").strip()
        if beat not in summary:
            summary[beat] = {"total": 0, "balance_sum": Decimal("0"), "by_salesman": {}}
        summary[beat]["total"] += 1
        summary[beat]["balance_sum"] += bal
        summary[beat]["by_salesman"][salesman] = summary[beat]["by_salesman"].get(salesman, 0) + 1
    return summary


def _load_vouchers_by_criterion(selection_type, selection_values, current_user=None):
    """Return pending vouchers filtered by beat, salesman, or both (beat_salesman)."""
    today = datetime.now().strftime("%Y-%m-%d")
    selected = set(selection_values)
    vouchers = []

    for row in load_vouchers_raw():
        row_beat = row.get("beat", "").strip()
        row_salesman = row.get("salesman", "").strip()

        if selection_type == "beat" and row_beat not in selected:
            continue
        if selection_type == "salesman" and row_salesman not in selected:
            continue
        if selection_type == "beat_salesman":
            if row_beat != selection_values[0] or row_salesman != selection_values[1]:
                continue

        balance_str = row.get("balance", "0").strip()
        try:
            balance = Decimal(balance_str)
        except Exception:
            continue

        if balance > 0:
            vouchers.append({
                "bill_no": row.get("bill_no", "").strip(),
                "voucher_date": row.get("date", "").strip(),
                "date": today,
                "balance": str(balance),
                "payment": "",
                "beat": row_beat,
                "salesman": row_salesman,
            })
    return vouchers


def _find_confirmed_start_report(selection_type, selection_values):
    """Return (path, data) for a confirmed start report matching the selection, or None."""
    if not STAGING_DIR.exists():
        return None
    selection_set = set(selection_values)
    for path in sorted(STAGING_DIR.glob("coll*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        stages = data.get("stages", {})
        start_confirmed = (
            stages.get("start") == "confirmed"
            or (data.get("stage") == "start" and data.get("status") == "confirmed")
        )
        if not start_confirmed or stages.get("submit") == "confirmed":
            continue
        if data.get("selection_type") != selection_type:
            continue
        if set(data.get("selection", [])) == selection_set:
            return path, data
    return None


def _find_any_active_beat_report(selection_type, selection_values):
    """Return (path, data) for any active staging report matching the selection, or None.
    Covers all pipeline stages: start, submit-inprogress, submit-confirmed (pre-finalize).
    """
    if not STAGING_DIR.exists():
        return None
    selection_set = set(selection_values)
    for path in sorted(STAGING_DIR.glob("coll*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("selection_type") != selection_type:
            continue
        if set(data.get("selection", [])) == selection_set:
            return path, data
    return None


def _load_confirmed_start_reports():
    """Return list of (path, data) for all stage:start status:confirmed reports in staging."""
    if not STAGING_DIR.exists():
        return []
    result = []
    for path in sorted(STAGING_DIR.glob("coll*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        stages = data.get("stages", {})
        start_confirmed = (
            stages.get("start") == "confirmed"
            or (data.get("stage") == "start" and data.get("status") == "confirmed")
        )
        if start_confirmed and stages.get("submit") != "confirmed":
            result.append((path, data))
    return result


def _load_submit_confirmed_reports():
    """Return (path, data) pairs where submit is confirmed and finalize not yet done."""
    if not STAGING_DIR.exists():
        return []
    result = []
    for path in sorted(STAGING_DIR.glob("coll*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        stages = data.get("stages", {})
        submit_confirmed = (
            stages.get("submit") == "confirmed"
            or (data.get("stage") == "submit" and data.get("status") == "confirmed")
        )
        if submit_confirmed and stages.get("finalize") != "confirmed":
            result.append((path, data))
    return result


# --- Report query functions ---

def query_pending_by_salesman(salesman, current_user=None):
    """Return dict[beat -> list[voucher_dict]] of pending vouchers for a salesman."""
    grouped = {}
    for row in load_vouchers_raw():
        if row.get("salesman", "").strip() != salesman:
            continue
        try:
            balance = Decimal(row.get("balance", "0").strip())
        except Exception:
            continue
        if balance <= 0:
            continue
        beat = row.get("beat", "").strip()
        grouped.setdefault(beat, []).append({
            "bill_no": row.get("bill_no", "").strip(),
            "date": row.get("date", "").strip(),
            "balance": str(balance),
        })
    return grouped


def query_pending_by_beat(beat, current_user=None):
    """Return dict[salesman -> list[voucher_dict]] of pending vouchers for a beat."""
    grouped = {}
    for row in load_vouchers_raw():
        if row.get("beat", "").strip() != beat:
            continue
        try:
            balance = Decimal(row.get("balance", "0").strip())
        except Exception:
            continue
        if balance <= 0:
            continue
        salesman = row.get("salesman", "").strip()
        grouped.setdefault(salesman, []).append({
            "bill_no": row.get("bill_no", "").strip(),
            "date": row.get("date", "").strip(),
            "balance": str(balance),
        })
    return grouped


def query_pending_by_age(limit, current_user=None):
    """Return (top_vouchers, total_count) — top `limit` pending vouchers oldest first with age field."""
    today_date = datetime.now().date()
    pending = []
    for row in load_vouchers_raw():
        try:
            balance = Decimal(row.get("balance", "0").strip())
        except Exception:
            continue
        if balance <= 0:
            continue
        date_str = row.get("date", "").strip()
        try:
            age = (today_date - datetime.strptime(date_str, "%Y-%m-%d").date()).days
        except Exception:
            age = 0
        pending.append({
            "bill_no": row.get("bill_no", "").strip(),
            "date": date_str,
            "balance": str(balance),
            "beat": row.get("beat", "").strip(),
            "salesman": row.get("salesman", "").strip(),
            "age": age,
        })
    pending.sort(key=lambda v: v["date"])
    return pending[:limit], len(pending)


def query_pending_by_amount(limit, current_user=None):
    """Return (top_vouchers, total_count) — top `limit` pending vouchers by balance descending."""
    pending = []
    for row in load_vouchers_raw():
        try:
            balance = Decimal(row.get("balance", "0").strip())
        except Exception:
            continue
        if balance <= 0:
            continue
        pending.append({
            "bill_no": row.get("bill_no", "").strip(),
            "date": row.get("date", "").strip(),
            "balance": balance,
            "beat": row.get("beat", "").strip(),
            "salesman": row.get("salesman", "").strip(),
        })
    pending.sort(key=lambda v: v["balance"], reverse=True)
    top = pending[:limit]
    for v in top:
        v["balance"] = str(v["balance"])
    return top, len(pending)


def _read_installments_for_bill(bill_no, inst_file):
    """Read installment rows matching bill_no from a CSV file."""
    if not inst_file.exists():
        return []
    rows = []
    with inst_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("bill_no", "").strip() == bill_no:
                rows.append(row)
    return rows


def search_voucher(bill_no):
    """Search active and completed vouchers by bill_no.

    Returns (voucher_dict, [installment_dicts], is_completed) or None if not found.
    Installment dicts use flexible keys — callers should use .get() with fallbacks.
    """
    bill_no = bill_no.strip()

    # Search active vouchers first
    for row in load_vouchers_raw():
        if row.get("bill_no", "").strip() == bill_no:
            voucher = {
                "bill_no": row.get("bill_no", "").strip(),
                "date": row.get("date", "").strip(),
                "amount": row.get("amount", "").strip(),
                "balance": row.get("balance", "").strip(),
                "beat": row.get("beat", "").strip(),
                "salesman": row.get("salesman", "").strip(),
            }
            installments = _read_installments_for_bill(bill_no, DATA_DIR / "installments.csv")
            return voucher, installments, False

    # Search completed vouchers
    completed_file = DATA_DIR / "completed_vouchers.csv"
    if completed_file.exists():
        with completed_file.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("bill_no", "").strip() == bill_no:
                    voucher = {
                        "bill_no": row.get("bill_no", "").strip(),
                        "date": row.get("date", "").strip(),
                        "amount": row.get("amount", "").strip(),
                        "balance": row.get("balance", "").strip(),
                        "beat": row.get("beat", "").strip(),
                        "salesman": row.get("salesman", "").strip(),
                    }
                    installments = _read_installments_for_bill(
                        bill_no, DATA_DIR / "completed_installments.csv"
                    )
                    return voucher, installments, True

    return None
