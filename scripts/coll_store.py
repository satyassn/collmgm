"""
File I/O and persistence layer for collection management.

Owns all path constants, CSV reads/writes, and JSON reads/writes.
No print() or input() calls. No imports from other coll_* modules.
"""

import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STAGING_DIR = ROOT_DIR / "staging"
ARCHIVE_DIR = ROOT_DIR / "archive"


def ensure_staging_dir():
    STAGING_DIR.mkdir(parents=True, exist_ok=True)


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


def load_vouchers_raw():
    """Read all rows from vouchers.csv as a list of dicts."""
    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        raise FileNotFoundError(f"Missing vouchers file: {vouchers_file}")
    with vouchers_file.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_collection_text(path, beats, salesmen, vouchers, stage=None, status=None):
    if not vouchers:
        raise ValueError("No vouchers available to write to text report.")

    date_str = datetime.now().strftime("%Y-%m-%d")
    bill_width = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    vdate_width = max(len("voucher_date"), max(len(v.get("voucher_date", "")) for v in vouchers))
    balance_width = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    payment_width = max(len("collection"), max(len(v.get("payment", "")) for v in vouchers))
    beat_width = max(len("beat"), max(len(v["beat"]) for v in vouchers))
    salesman_width = max(len("salesman"), max(len(v["salesman"]) for v in vouchers))

    header = (
        f"{ 'bill_no':<{bill_width}}  "
        f"{ 'voucher_date':<{vdate_width}}  "
        f"{ 'balance':>{balance_width}}  "
        f"{ 'collection':>{payment_width}}  "
        f"{ 'beat':<{beat_width}}  "
        f"{ 'salesman':<{salesman_width}}"
    )
    separator = "-" * len(header)

    lines = ["COLLECTION REPORT"]
    if stage is not None:
        lines.append(f"Stage : {stage}")
    if status is not None:
        lines.append(f"Status: {status}")
    lines += [
        f"Beats: {', '.join(beats)}",
        f"Salesmen: {', '.join(salesmen)}",
        f"Collection date: {date_str}",
        "",
        header,
        separator,
    ]

    for voucher in vouchers:
        lines.append(
            f"{voucher['bill_no']:<{bill_width}}  "
            f"{voucher.get('voucher_date', ''):<{vdate_width}}  "
            f"{voucher['balance']:>{balance_width}}  "
            f"{voucher.get('payment', ''):>{payment_width}}  "
            f"{voucher['beat']:<{beat_width}}  "
            f"{voucher['salesman']:<{salesman_width}}"
        )

    lines.append(separator)
    total_vouchers = len(vouchers)
    total_balance = sum(Decimal(v["balance"]) for v in vouchers)
    total_payments = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
    lines.append(f"Total vouchers: {total_vouchers}")
    lines.append(f"Sum of coll: {total_balance}")
    if total_payments > 0:
        lines.append(f"Total payments entered: {total_payments}")

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def sanitize_filename_component(value):
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())
    return safe or "unknown"


def list_staging_reports():
    if not STAGING_DIR.exists():
        return []
    return sorted(STAGING_DIR.glob("coll*.json"))


def _installments_path(report_path):
    return report_path.parent / f"{report_path.stem}-installments.json"


def _save_installments(report_path, vouchers, bookmark_bill_no=None, inst_status=None):
    data = {v["bill_no"]: v["payment"] for v in vouchers if v.get("payment")}
    if bookmark_bill_no:
        data["__bookmark__"] = bookmark_bill_no
    if inst_status:
        data["__status__"] = inst_status
    with _installments_path(report_path).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _load_installments(report_path):
    path = _installments_path(report_path)
    if not path.exists():
        return {}, None, None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        bookmark = data.pop("__bookmark__", None)
        status = data.pop("__status__", None)
        return data, bookmark, status
    except Exception:
        return {}, None, None


def _append_installments_csv(vouchers):
    inst_file = DATA_DIR / "installments.csv"
    collection_date = datetime.now().strftime("%Y-%m-%d")
    write_header = not inst_file.exists()
    with inst_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["bill_no", "voucher_date", "collection_date", "payment", "beat", "salesman"])
        if write_header:
            writer.writeheader()
        for v in vouchers:
            if v.get("payment"):
                writer.writerow({
                    "bill_no": v["bill_no"],
                    "voucher_date": v.get("voucher_date", ""),
                    "collection_date": collection_date,
                    "payment": v["payment"],
                    "beat": v["beat"],
                    "salesman": v["salesman"],
                })


def _update_vouchers_balance(vouchers):
    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        raise FileNotFoundError(f"Missing vouchers file: {vouchers_file}")

    payment_map = {
        v["bill_no"]: Decimal(v["payment"])
        for v in vouchers
        if v.get("payment")
    }
    if not payment_map:
        return

    rows = []
    fieldnames = None
    with vouchers_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            bill_no = row.get("bill_no", "").strip()
            if bill_no in payment_map:
                try:
                    old_balance = Decimal(row["balance"].strip())
                    new_balance = max(Decimal("0"), old_balance - payment_map[bill_no])
                    row["balance"] = str(new_balance.quantize(Decimal("0.01")))
                except Exception:
                    pass
            rows.append(row)

    with vouchers_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
