"""
Data loading and query layer for collection management.

Reads master CSVs and staging JSON files. No print() or input() calls.
Imports only from coll_store. current_user parameter is reserved for future RBAC.
"""

import csv
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from coll_store import (
    DATA_DIR, STAGING_DIR, load_vouchers_raw, bill_no_sort_key,
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
        except (ValueError, InvalidOperation):
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

def validate_single_voucher(bill_no, date_str, amount_str, beat, salesman,
                             existing_bill_nos, valid_beats, valid_salesmen):
    """Validate one voucher's fields. Returns (errors: list[str], amount: Decimal or None)."""
    errors = []
    amount = None

    if not bill_no:
        errors.append("Bill No is required.")
    elif bill_no in existing_bill_nos:
        errors.append(f"Bill No '{bill_no}' already exists in the system.")

    if not date_str:
        errors.append("Date is required.")
    else:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            errors.append(f"Invalid date '{date_str}'; expected YYYY-MM-DD.")

    if not amount_str:
        errors.append("Amount is required.")
    else:
        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                errors.append("Amount must be positive.")
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

    return errors, amount


def validate_addv_batch(voucher_rows, inst_rows, existing_bill_nos,
                        valid_beats, valid_salesmen, created_by, now_str):
    """Validate batch CSV data. Returns (errors, vouchers, installments).

    Errors block the entire import. Vouchers/installments are enriched with metadata
    and balance is calculated from installment sums.
    """
    errors = []
    vouchers = []
    seen_bill_nos = set()

    for i, row in enumerate(voucher_rows, start=2):
        bill_no = row.get("bill_no", "").strip()
        date_str = row.get("date", "").strip()
        amount_str = row.get("amount", "").strip()
        beat = row.get("beat", "").strip()
        salesman = row.get("salesman", "").strip()

        if bill_no and bill_no in seen_bill_nos:
            errors.append(f"Voucher row {i}: duplicate bill_no '{bill_no}' in import file.")
            continue

        row_errors, amount = validate_single_voucher(
            bill_no, date_str, amount_str, beat, salesman,
            existing_bill_nos, valid_beats, valid_salesmen,
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
        bill_no = row.get("bill_no", "").strip()
        date_str = row.get("date", "").strip()
        amount_str = row.get("amount", "").strip()
        salesman = row.get("salesman", "").strip()

        if not bill_no:
            errors.append(f"Installment row {i}: bill_no is empty.")
            continue
        if bill_no not in seen_bill_nos:
            errors.append(f"Installment row {i}: bill_no '{bill_no}' not in vouchers being imported.")
            continue
        if not date_str:
            errors.append(f"Installment row {i} ({bill_no}): date is empty.")
            continue
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
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
        if inst_amount <= 0:
            errors.append(f"Installment row {i} ({bill_no}): amount must be positive.")
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
