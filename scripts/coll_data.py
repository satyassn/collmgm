"""
Data loading and query layer for collection management.

Reads master CSVs and staging JSON files. No print() or input() calls.
Imports only from coll_store. current_user parameter is reserved for future RBAC.
"""

import json
import re
from collections import namedtuple
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from coll_store import (
    DATA_DIR, STAGING_DIR, load_vouchers_raw, bill_no_sort_key,
    load_beats_raw, load_users_raw,
    load_installments_for_bill, load_completed_voucher,
)

NUMOF_TOP_AGED_VOUCHERS = 10
NUMOF_TOP_AMOUNT_VOUCHERS = 10


def load_beats(current_user=None):
    """Return list of beat names from the beats table.

    For salesman role, returns only beats assigned to that salesman
    (beats.salesman column).
    """
    rows = load_beats_raw()
    if current_user and current_user.role == 'salesman':
        beats = [r["name"] for r in rows
                 if r.get("name", "").strip() and r.get("salesman", "").strip() == current_user.name]
    else:
        beats = [r["name"] for r in rows if r.get("name", "").strip()]
    if not beats:
        raise ValueError("No beats found in database.")
    return beats


def load_salesmen(current_user=None):
    """Return list of salesman names from the users table.

    For salesman role, returns only the current user's own name.
    """
    if current_user and current_user.role == 'salesman':
        return [current_user.name]

    rows = load_users_raw()
    salesmen = [r["name"] for r in rows if r.get("role", "").strip().lower() == "salesman" and r.get("name", "").strip()]
    if not salesmen:
        raise ValueError("No salesmen found in database.")
    return salesmen


def load_beats_pending_summary(current_user=None):
    """Return dict[beat -> {"total": int, "balance_sum": Decimal, "by_salesman": dict}] for pending vouchers.

    When current_user is a salesman, only that salesman's vouchers are counted.
    """
    rows = load_vouchers_raw()
    filter_salesman = current_user.name if (current_user and current_user.role == 'salesman') else None
    summary = {}
    for row in rows:
        beat = row.get("beat", "").strip()
        if not beat:
            continue
        salesman = row.get("salesman", "").strip()
        if filter_salesman and salesman != filter_salesman:
            continue
        try:
            bal = Decimal(row.get("balance", "0").strip())
        except (ValueError, InvalidOperation):
            continue
        if bal <= 0:
            continue
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
        except (ValueError, InvalidOperation):
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
    return sorted(vouchers, key=lambda v: bill_no_sort_key(v["bill_no"]))


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
        if stages.get("start") != "confirmed" or stages.get("submit") == "confirmed":
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


def load_active_beat_statuses():
    """Return dict[beat -> status_label] for every beat that has an active report in staging/."""
    if not STAGING_DIR.exists():
        return {}
    result = {}
    for path in sorted(STAGING_DIR.glob("coll*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        stages = data.get("stages", {})
        if stages.get("post") == "confirmed":
            continue
        sel = data.get("selection", [])
        beat = sel[0] if sel else None
        if not beat:
            continue
        if stages.get("submit") == "confirmed":
            label = "submit approved"
        elif stages.get("submit") in ("submitted", "inprogress"):
            label = "submit in progress"
        elif stages.get("submit") == "returned":
            label = "return requested"
        elif stages.get("start") == "confirmed":
            label = "start approved"
        else:
            label = "awaiting approval"
        result[beat] = label
    return result


def _load_confirmed_start_reports():
    """Return (path, data) pairs where start is confirmed and submit not yet started or in progress."""
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
        if stages.get("start") != "confirmed":
            continue
        if stages.get("submit") in ("submitted", "confirmed"):
            continue
        result.append((path, data))
    return result


def _load_submit_confirmed_reports():
    """Return (path, data) pairs where submit is confirmed and post not yet done."""
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
        if stages.get("submit") == "confirmed" and stages.get("post") != "confirmed":
            result.append((path, data))
    return result


# --- Report query functions ---

def _report_row(row, balance):
    """Common voucher fields for the pending-voucher reports. `amount` and `paid`
    (amount - balance) let the minimal voucher card show Amt / Paid / Bal."""
    amount = row.get("amount", "").strip()
    try:
        paid = str(Decimal(amount) - balance)
    except (ValueError, InvalidOperation):
        amount, paid = "", ""
    return {
        "bill_no": row.get("bill_no", "").strip(),
        "date": row.get("date", "").strip(),
        "amount": amount,
        "paid": paid,
        "balance": str(balance),
        "beat": row.get("beat", "").strip(),
        "salesman": row.get("salesman", "").strip(),
    }


def query_pending_by_salesman(salesman, current_user=None):
    """Return dict[beat -> list[voucher_dict]] of pending vouchers for a salesman."""
    grouped = {}
    for row in load_vouchers_raw():
        if row.get("salesman", "").strip() != salesman:
            continue
        try:
            balance = Decimal(row.get("balance", "0").strip())
        except (ValueError, InvalidOperation):
            continue
        if balance <= 0:
            continue
        beat = row.get("beat", "").strip()
        grouped.setdefault(beat, []).append(_report_row(row, balance))
    return grouped


def query_pending_by_beat(beat, current_user=None):
    """Return dict[salesman -> list[voucher_dict]] of pending vouchers for a beat."""
    grouped = {}
    for row in load_vouchers_raw():
        if row.get("beat", "").strip() != beat:
            continue
        try:
            balance = Decimal(row.get("balance", "0").strip())
        except (ValueError, InvalidOperation):
            continue
        if balance <= 0:
            continue
        salesman = row.get("salesman", "").strip()
        grouped.setdefault(salesman, []).append(_report_row(row, balance))
    return grouped


def query_pending_by_age(limit, current_user=None):
    """Return (top_vouchers, total_count) — top `limit` pending vouchers oldest first with age field."""
    today_date = datetime.now().date()
    salesman_filter = current_user.name if current_user and current_user.role == 'salesman' else None
    pending = []
    for row in load_vouchers_raw():
        if salesman_filter and row.get("salesman", "").strip() != salesman_filter:
            continue
        try:
            balance = Decimal(row.get("balance", "0").strip())
        except (ValueError, InvalidOperation):
            continue
        if balance <= 0:
            continue
        date_str = row.get("date", "").strip()
        try:
            age = (today_date - datetime.strptime(date_str, "%Y-%m-%d").date()).days
        except ValueError:
            age = 0
        entry = _report_row(row, balance)
        entry["age"] = age
        pending.append(entry)
    pending.sort(key=lambda v: v["date"])
    return pending[:limit], len(pending)


def query_pending_by_amount(limit, current_user=None):
    """Return (top_vouchers, total_count) — top `limit` pending vouchers by balance descending."""
    salesman_filter = current_user.name if current_user and current_user.role == 'salesman' else None
    pending = []
    for row in load_vouchers_raw():
        if salesman_filter and row.get("salesman", "").strip() != salesman_filter:
            continue
        try:
            balance = Decimal(row.get("balance", "0").strip())
        except (ValueError, InvalidOperation):
            continue
        if balance <= 0:
            continue
        entry = _report_row(row, balance)
        entry["_sort"] = balance
        pending.append(entry)
    pending.sort(key=lambda v: v["_sort"], reverse=True)
    top = pending[:limit]
    for v in top:
        del v["_sort"]
    return top, len(pending)


def search_voucher(bill_no):
    """Search active and completed vouchers by bill_no.

    Returns (voucher_dict, [installment_dicts], is_completed) or None if not found.
    """
    bill_no = bill_no.strip().upper()

    for row in load_vouchers_raw():
        if row.get("bill_no", "").strip() == bill_no:
            voucher = {k: row.get(k, "").strip() for k in ("bill_no", "date", "amount", "balance", "beat", "salesman")}
            return voucher, load_installments_for_bill(bill_no, completed=False), False

    row = load_completed_voucher(bill_no)
    if row:
        voucher = {k: row.get(k, "").strip() for k in ("bill_no", "date", "amount", "balance", "beat", "salesman")}
        return voucher, load_installments_for_bill(bill_no, completed=True), True

    return None


def load_voucher_amounts(bill_nos):
    """Map bill_no -> amount string for the given bills (active vouchers only)."""
    wanted = {b.strip() for b in bill_nos}
    return {
        row.get("bill_no", "").strip(): row.get("amount", "").strip()
        for row in load_vouchers_raw()
        if row.get("bill_no", "").strip() in wanted
    }


# --- Add-vouchers query ---

def load_addv_pending_confirm_by_beat():
    """Group pending addv staging reports by beat for supervisor confirmation.

    Returns dict[beat -> {"files": [(path, data)], "vouchers": [...], "installments": [...]}].
    A staging file appears under every beat its vouchers belong to.
    """
    grouped = {}
    seen_paths = {}

    if not STAGING_DIR.exists():
        return grouped

    for path in sorted(STAGING_DIR.glob("addv*.json")):
        try:
            with path.open(encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        stages = data.get("stages", {})
        if stages.get("add") != "done" or stages.get("confirm") == "confirmed":
            continue

        bill_beat = {v.get("bill_no", ""): v.get("beat", "") for v in data.get("vouchers", [])}

        for v in data.get("vouchers", []):
            beat = v.get("beat", "")
            if not beat:
                continue
            if beat not in grouped:
                grouped[beat] = {"files": [], "vouchers": [], "installments": []}
                seen_paths[beat] = set()
            if path not in seen_paths[beat]:
                grouped[beat]["files"].append((path, data))
                seen_paths[beat].add(path)
            grouped[beat]["vouchers"].append(v)

        for inst in data.get("installments", []):
            beat = bill_beat.get(inst.get("bill_no", ""), "")
            if beat in grouped:
                grouped[beat]["installments"].append(inst)

    return grouped


# --- Add-vouchers validation ---

# First char alphanumeric blocks spreadsheet formula injection (=+-@);
# the allowlist excludes commas/quotes/whitespace that break CSV exports.
# Public (not _-prefixed): also used by coll_api.py to pre-validate a
# voucher-number change in Voucher Amendment before calling into coll_orchestrate.
BILL_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def validate_single_voucher(bill_no, date_str, amount_str, beat, salesman,
                             existing_bill_nos, valid_beats, valid_salesmen,
                             beat_salesman_map=None):
    """Validate one voucher's fields. Returns (errors: list[str], amount: Decimal or None).

    beat_salesman_map: optional {beat_name: assigned_salesman}. When given and
    the beat has an assigned salesman on file, a voucher whose own salesman
    differs is rejected here — importing a mismatch is blocked outright,
    rather than left to surface later as a blocked Generate Collection List.
    A beat with no assignment yet is not checked; there is nothing to compare
    against.
    """
    errors = []
    amount = None

    if not bill_no:
        errors.append("Voucher is required.")
    elif not BILL_NO_RE.match(bill_no):
        errors.append(f"Voucher '{bill_no}' contains invalid characters "
                      "(allowed: letters, digits, . _ / - ; must start with a letter or digit).")
    elif bill_no in existing_bill_nos:
        errors.append(f"Voucher '{bill_no}' already exists in the system.")

    if not date_str:
        errors.append("Date is required.")
    else:
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
            if parsed > datetime.now().date():
                errors.append(f"Date '{date_str}' is in the future.")
        except ValueError:
            errors.append(f"Invalid date '{date_str}'; expected YYYY-MM-DD.")

    if not amount_str:
        errors.append("Amount is required.")
    else:
        try:
            amount = Decimal(amount_str)
            if not amount.is_finite() or amount <= 0:
                errors.append("Amount must be a positive number.")
                amount = None
        except InvalidOperation:
            errors.append(f"Invalid amount '{amount_str}'.")

    if not beat:
        errors.append("Beat is required.")
    elif beat not in valid_beats:
        errors.append(f"Beat '{beat}' not found in beats.csv.")

    if not salesman:
        errors.append("Salesman is required.")
    elif salesman not in valid_salesmen:
        errors.append(f"Salesman '{salesman}' not found in users.csv.")
    elif beat_salesman_map is not None:
        assigned = beat_salesman_map.get(beat, "")
        if assigned and salesman != assigned:
            errors.append(
                f"Salesman '{salesman}' does not match the assigned salesman "
                f"'{assigned}' for beat '{beat}'.")

    return errors, amount


def validate_addv_batch(voucher_rows, inst_rows, existing_bill_nos,
                        valid_beats, valid_salesmen, created_by, now_str,
                        beat_salesman_map=None):
    """Validate batch CSV data. Returns (errors, vouchers, installments).

    Errors block the entire import. Vouchers/installments are enriched with metadata
    and balance is calculated from installment sums. beat_salesman_map: see
    validate_single_voucher — blocks a row whose salesman doesn't match its
    beat's assigned salesman.
    """
    errors = []
    vouchers = []
    seen_bill_nos = set()

    for i, row in enumerate(voucher_rows, start=2):
        bill_no = row.get("bill_no", "").strip().upper()
        date_str = row.get("date", "").strip()
        amount_str = row.get("amount", "").strip()
        beat = row.get("beat", "").strip()
        salesman = row.get("salesman", "").strip()

        if bill_no and bill_no in seen_bill_nos:
            errors.append(f"Voucher row {i}: duplicate voucher '{bill_no}' in import file.")
            continue

        row_errors, amount = validate_single_voucher(
            bill_no, date_str, amount_str, beat, salesman,
            existing_bill_nos, valid_beats, valid_salesmen,
            beat_salesman_map=beat_salesman_map,
        )
        if row_errors:
            for e in row_errors:
                errors.append(f"Voucher row {i}: {e}")
            continue

        seen_bill_nos.add(bill_no)
        vouchers.append({
            "bill_no": bill_no,
            "date": date_str,
            "amount": str(amount.quantize(Decimal("0.01"))),
            "balance": str(amount.quantize(Decimal("0.01"))),
            "beat": beat,
            "salesman": salesman,
            "created_by": created_by,
            "created_at": now_str,
        })

    installments = []
    inst_sums = {}

    for i, row in enumerate(inst_rows, start=2):
        bill_no = row.get("bill_no", "").strip().upper()
        date_str = row.get("date", "").strip()
        amount_str = row.get("amount", "").strip()
        salesman = row.get("salesman", "").strip()

        if not bill_no:
            errors.append(f"Installment row {i}: voucher is empty.")
            continue
        if bill_no not in seen_bill_nos:
            errors.append(f"Installment row {i}: voucher '{bill_no}' not in vouchers being imported.")
            continue
        if not date_str:
            errors.append(f"Installment row {i} ({bill_no}): date is empty.")
            continue
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
            if parsed > datetime.now().date():
                errors.append(f"Installment row {i} ({bill_no}): date '{date_str}' is in the future.")
                continue
        except ValueError:
            errors.append(f"Installment row {i} ({bill_no}): invalid date '{date_str}'.")
            continue
        if not amount_str:
            errors.append(f"Installment row {i} ({bill_no}): amount is empty.")
            continue
        try:
            inst_amount = Decimal(amount_str)
        except InvalidOperation:
            errors.append(f"Installment row {i} ({bill_no}): invalid amount '{amount_str}'.")
            continue
        if not inst_amount.is_finite() or inst_amount <= 0:
            errors.append(f"Installment row {i} ({bill_no}): amount must be a positive number.")
            continue
        if not salesman:
            errors.append(f"Installment row {i} ({bill_no}): salesman is empty.")
            continue
        if salesman not in valid_salesmen:
            errors.append(f"Installment row {i} ({bill_no}): salesman '{salesman}' not found in users.csv.")
            continue

        inst_sums[bill_no] = inst_sums.get(bill_no, Decimal("0")) + inst_amount
        installments.append({
            "bill_no": bill_no,
            "date": date_str,
            "amount": str(inst_amount.quantize(Decimal("0.01"))),
            "salesman": salesman,
            "created_by": created_by,
            "created_at": now_str,
        })

    for v in vouchers:
        bill_no = v["bill_no"]
        inst_sum = inst_sums.get(bill_no, Decimal("0"))
        v_amount = Decimal(v["amount"])
        if inst_sum > v_amount:
            errors.append(
                f"Voucher '{bill_no}': total installments ({inst_sum}) exceed amount ({v_amount})."
            )
        else:
            v["balance"] = str((v_amount - inst_sum).quantize(Decimal("0.01")))

    return errors, vouchers, installments


# --- Beat cadence and due dates (pure) ---

CADENCE_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
CADENCE_WEEKS = ("1", "2", "3", "4")
DUE_WINDOW_DEFAULT = 3
DUE_WINDOW_MAX = 14
_LIST_STAMP_RE = re.compile(r"^coll(\d{4})(\d{2})(\d{2})")


def _parse_iso_date(text):
    """YYYY-MM-DD -> date, or None for blank/invalid input."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def report_date(path, data):
    """Date a collection list was generated: its `date` field, else the
    YYYYMMDD stamp in its filename (older lists predate the field)."""
    parsed = _parse_iso_date(data.get("date", ""))
    if parsed:
        return parsed
    m = _LIST_STAMP_RE.match(path.name)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def next_weekday_on_or_after(d, weekday):
    """First date on or after `d` that falls on `weekday` (0 = Monday)."""
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def nth_weekday_of_month(year, month, weekday, n):
    """The n-th (1-4) occurrence of `weekday` (0=Mon) in the given month.
    Always valid for n<=4 — every month has at least four of each weekday."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _next_due_weekly(cadence_day, cadence_week, cadence_start, anchor, today):
    if cadence_day not in CADENCE_DAYS:
        return None
    weekday = CADENCE_DAYS[cadence_day]
    if anchor is not None:
        return next_weekday_on_or_after(anchor + timedelta(days=7), weekday)
    if cadence_start is not None:
        return cadence_start
    return next_weekday_on_or_after(today, weekday)


def _next_due_biweekly(cadence_day, cadence_week, cadence_start, anchor, today):
    if cadence_day not in CADENCE_DAYS:
        return None
    weekday = CADENCE_DAYS[cadence_day]
    if anchor is not None:
        return anchor + timedelta(days=14)  # +14 days preserves whatever weekday the anchor fell on
    if cadence_start is not None:
        return cadence_start
    return next_weekday_on_or_after(today, weekday)


def _next_due_monthly(cadence_day, cadence_week, cadence_start, anchor, today):
    if cadence_day not in CADENCE_DAYS or cadence_week not in CADENCE_WEEKS:
        return None
    weekday, n = CADENCE_DAYS[cadence_day], int(cadence_week)
    if anchor is not None:
        year, month = anchor.year, anchor.month + 1
        if month > 12:
            month, year = 1, year + 1
        return nth_weekday_of_month(year, month, weekday, n)
    if cadence_start is not None:
        return cadence_start
    year, month = today.year, today.month
    due = nth_weekday_of_month(year, month, weekday, n)
    if due < today:
        # This month's slot already passed; next month's is unconditionally
        # on/after today (any date in month+1 is after any date in month,
        # and today is still within `month`) — never needs a second check.
        month += 1
        if month > 12:
            month, year = 1, year + 1
        due = nth_weekday_of_month(year, month, weekday, n)
    return due


_WEEKDAY_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CADENCE_WEEK_LABELS = {"1": "1st", "2": "2nd", "3": "3rd", "4": "4th"}


def _validate_start_weekday(cadence_day, cadence_week, start):
    """Shared by weekly and biweekly: an explicit first collection date must
    actually fall on the chosen collection day."""
    if start.weekday() != CADENCE_DAYS[cadence_day]:
        return (f"First collection date {start.isoformat()} is a {_WEEKDAY_FULL[start.weekday()]}, "
                f"but the collection day is set to {_WEEKDAY_FULL[CADENCE_DAYS[cadence_day]]}.")
    return None


def _validate_start_monthly(cadence_day, cadence_week, start):
    """An explicit first collection date must be the exact chosen
    week-of-month occurrence of the chosen collection day."""
    weekday, n = CADENCE_DAYS[cadence_day], int(cadence_week)
    if nth_weekday_of_month(start.year, start.month, weekday, n) != start:
        return (f"First collection date {start.isoformat()} ({_WEEKDAY_FULL[start.weekday()]}) is not "
                f"the {CADENCE_WEEK_LABELS[cadence_week]} {_WEEKDAY_FULL[weekday]} of its month.")
    return None


CadenceSpec = namedtuple("CadenceSpec", ["required_fields", "next_due", "approx_days", "validate_start"])

# Adding a cadence means one new _next_due_* function + one entry here —
# next_due_date/validate_cadence/count_remaining_visits all dispatch off this,
# none of them hardcode a per-cadence branch. validate_start may be None for a
# cadence with no explicit-start-date cross-check to make.
CADENCE_REGISTRY = {
    "weekly":   CadenceSpec(frozenset({"day"}), _next_due_weekly, 7, _validate_start_weekday),
    "biweekly": CadenceSpec(frozenset({"day"}), _next_due_biweekly, 14, _validate_start_weekday),
    "monthly":  CadenceSpec(frozenset({"day", "week"}), _next_due_monthly, 30, _validate_start_monthly),
}
CADENCES = tuple(CADENCE_REGISTRY)


def validate_cadence(cadence, cadence_day, cadence_week, cadence_start):
    """Error message for an invalid cadence setting, or None.

    A weekly, biweekly or monthly beat all need a collection day; a monthly
    beat also needs which week of the month (1st-4th). cadence_start may be
    blank for any cadence — left blank, the next due date is auto-computed
    from today instead (a blank weekly/monthly beat already worked this way;
    this now applies to biweekly too). When cadence_start IS given, it must
    actually fall on the right day (and week, for monthly) for the cadence
    — otherwise it's rejected with a specific message, not silently accepted.
    """
    spec = CADENCE_REGISTRY.get(cadence)
    if spec is None:
        return f"Cadence must be one of: {', '.join(CADENCES)}."
    if "day" in spec.required_fields and cadence_day not in CADENCE_DAYS:
        return f"A {cadence} beat needs a collection day (Mon to Sun)."
    if "week" in spec.required_fields and cadence_week not in CADENCE_WEEKS:
        return f"A {cadence} beat needs a week of the month (1st to 4th)."
    if cadence_start:
        start = _parse_iso_date(cadence_start)
        if start is None:
            return f"Invalid start date '{cadence_start}'; expected YYYY-MM-DD."
        if spec.validate_start is not None:
            start_err = spec.validate_start(cadence_day, cadence_week, start)
            if start_err:
                return start_err
    return None


def next_due_date(cadence, cadence_day, cadence_week, cadence_start, anchor, today):
    """Next date a beat is due, or None when it cannot be worked out.

    anchor: the date of the beat's active collection list if it has one, else
    the date of its last posted list, else None (no history).
    cadence_start: an optional explicit first due date, used only when there's
    no anchor yet — otherwise every cadence auto-computes from cadence_day
    (and cadence_week, for monthly) relative to today.
    """
    spec = CADENCE_REGISTRY.get(cadence)
    if spec is None:
        return None
    return spec.next_due(cadence_day, cadence_week, cadence_start, anchor, today)


def latest_list_dates(reports):
    """beat -> newest list date, from (path, data) collection-list pairs.

    Public (not _-prefixed): also used by coll_api.py's Generate Collection
    List picker to show each beat's active list's start date.
    """
    latest = {}
    for path, data in reports:
        sel = data.get("selection") or []
        d = report_date(path, data)
        if not sel or d is None:
            continue
        if sel[0] not in latest or d > latest[sel[0]]:
            latest[sel[0]] = d
    return latest


def build_due_list(beats, staged, archived, today, window_days):
    """Split beats into overdue / due today / upcoming within `window_days`,
    plus beats that cannot be scheduled yet.

    beats: rows with name, salesman, cadence, cadence_day, cadence_week, cadence_start.
    staged / archived: (path, data) collection-list pairs. The anchor is the
    newest staged list for the beat if one is active, else its newest archived list.
    """
    active = latest_list_dates(staged)
    posted = latest_list_dates(archived)
    horizon = today + timedelta(days=window_days)
    result = {"overdue": [], "due_today": [], "upcoming": [], "needs_setup": []}
    for b in beats:
        name = b["name"]
        anchor = active.get(name) or posted.get(name)
        due = next_due_date(b.get("cadence", ""), b.get("cadence_day", ""),
                            b.get("cadence_week", ""), _parse_iso_date(b.get("cadence_start", "")),
                            anchor, today)
        row = {
            "beat": name,
            "salesman": b.get("salesman", ""),
            "cadence": b.get("cadence", ""),
            "cadence_day": b.get("cadence_day", ""),
            "cadence_week": b.get("cadence_week", ""),
            "next_due": due,
            "anchor": anchor,
            "in_progress": name in active,
        }
        if due is None:
            result["needs_setup"].append(row)
        elif due < today:
            result["overdue"].append(row)
        elif due == today:
            result["due_today"].append(row)
        elif due <= horizon:
            result["upcoming"].append(row)
    for key in ("overdue", "due_today", "upcoming"):
        result[key].sort(key=lambda r: (r["next_due"], r["beat"]))
    result["needs_setup"].sort(key=lambda r: r["beat"])
    return result


# --- Collection Health (pure) ---

VISIT_METHODS = ("accurate", "approximate")
LAST_VISIT_COLORS = ("yellow", "red")


def validate_collection_health_settings(credit_period_days_str, visit_method, last_visit_color):
    """Error messages (if any) for the Collection Health settings form, plus
    the parsed credit period. Returns (errors: list[str], credit_period_days: int or None)."""
    errors = []
    credit_period_days = None
    try:
        credit_period_days = int(credit_period_days_str)
        if credit_period_days <= 0:
            errors.append("Credit period must be a positive number of days.")
            credit_period_days = None
    except (TypeError, ValueError):
        errors.append(f"Invalid credit period {credit_period_days_str!r}; expected a whole number of days.")
    if visit_method not in VISIT_METHODS:
        errors.append("Invalid visit counting method.")
    if last_visit_color not in LAST_VISIT_COLORS:
        errors.append("Invalid choice for the last-chance visit color.")
    return errors, credit_period_days


def count_remaining_visits(cadence, cadence_day, cadence_week, cadence_start, anchor, today,
                            deadline, method="accurate", max_iterations=120):
    """Number of scheduled beat visits on/after `today` and on/before
    `deadline`. None if the beat's cadence can't be scheduled (needs setup).

    method="approximate" ignores cadence_day/cadence_week/cadence_start/anchor
    entirely — it only needs the cadence type to pick a flat day interval.
    """
    if method == "approximate":
        spec = CADENCE_REGISTRY.get(cadence)
        if spec is None:
            return None
        days_left = (deadline - today).days
        return max(days_left, 0) // spec.approx_days

    current_anchor = anchor
    visits = 0
    for _ in range(max_iterations):
        due = next_due_date(cadence, cadence_day, cadence_week, cadence_start, current_anchor, today)
        if due is None:
            return None
        if due < today:
            current_anchor = due  # stale anchor chain catching up to today; don't count
            continue
        if due > deadline:
            break
        visits += 1
        current_anchor = due
    return visits


def classify_voucher_health(voucher_date, credit_period_days, cadence, cadence_day, cadence_week,
                             cadence_start, anchor, today, visit_method="accurate",
                             last_visit_color="yellow"):
    """'red' | 'yellow' | 'green' | 'unknown' for one open voucher.

    'unknown' means the beat's cadence can't be scheduled yet (needs setup,
    same condition as Beats Due) — only possible under visit_method="accurate".

    last_visit_color decides how the ONE visit that is the last realistic
    chance to pay within the credit period is colored, and shifts the whole
    green/yellow boundary to match it:
    "yellow" (default): >=2 visits remaining is green, 1 is yellow, and 0 is
    also yellow (never shown as calmer than "exactly one left").
    "red": >=3 is green, 2 is yellow (one visit before the last chance), and
    1 or 0 is red (the last-chance visit itself, marked red right away
    instead of waiting for the credit period to actually expire).
    """
    age = (today - voucher_date).days
    if age > credit_period_days:
        return "red"
    deadline = voucher_date + timedelta(days=credit_period_days)
    visits = count_remaining_visits(cadence, cadence_day, cadence_week, cadence_start, anchor,
                                     today, deadline, method=visit_method)
    if visits is None:
        return "unknown"
    last_chance = 1 if last_visit_color == "yellow" else 2
    if visits > last_chance:
        return "green"
    if visits == last_chance:
        return "yellow"
    return last_visit_color


def build_collection_health(beat_row, vouchers, settings, staged, archived, today):
    """Classify a beat's open vouchers into green/yellow/red/unknown.

    vouchers: flat list of voucher dicts for this beat, already filtered to
    balance > 0 (pass query_pending_by_beat(beat_row["name"]).values() flattened).
    settings: the dict from coll_store.get_collection_health_settings().
    staged / archived: (path, data) collection-list pairs, same shape build_due_list takes.
    """
    anchor = (latest_list_dates(staged).get(beat_row["name"])
              or latest_list_dates(archived).get(beat_row["name"]))
    buckets = {"green": [], "yellow": [], "red": [], "unknown": []}
    for v in vouchers:
        vd = _parse_iso_date(v["date"])
        bucket = classify_voucher_health(
            vd, settings["credit_period_days"], beat_row.get("cadence", ""),
            beat_row.get("cadence_day", ""), beat_row.get("cadence_week", ""),
            _parse_iso_date(beat_row.get("cadence_start", "")),
            anchor, today, visit_method=settings["visit_method"],
            last_visit_color=settings["last_visit_color"])
        buckets[bucket].append(v)
    counts = {k: len(v) for k, v in buckets.items()}
    totals = {k: sum((Decimal(x["balance"]) for x in v), Decimal("0")) for k, v in buckets.items()}
    return {"buckets": buckets, "counts": counts, "totals": totals, "anchor": anchor}
