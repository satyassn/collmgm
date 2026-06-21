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
PRINTS_DIR = ROOT_DIR / "prints"

_PRINT_COL_WIDTH = 28
_PRINT_COL_SEP = " | "
_PRINT_PAGE_HEIGHT = 66
_PRINT_FILE_HEADER_LINES = 3


def ensure_staging_dir():
    STAGING_DIR.mkdir(parents=True, exist_ok=True)


def ensure_prints_dir():
    PRINTS_DIR.mkdir(parents=True, exist_ok=True)


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

    header = (
        f"{ 'bill_no':<{bill_width}}  "
        f"{ 'voucher_date':<{vdate_width}}  "
        f"{ 'balance':>{balance_width}}  "
        f"{ 'collection':>{payment_width}}"
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
            f"{voucher.get('payment', ''):>{payment_width}}"
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
    created_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    write_header = not inst_file.exists()
    with inst_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["bill_no", "date", "amount", "salesman", "created_by", "created_at"])
        if write_header:
            writer.writeheader()
        for v in vouchers:
            if v.get("payment"):
                writer.writerow({
                    "bill_no": v["bill_no"],
                    "date": collection_date,
                    "amount": v["payment"],
                    "salesman": v["salesman"],
                    "created_by": "app",
                    "created_at": created_at,
                })


def _update_vouchers_balance(vouchers):
    """Update balances in vouchers.csv. Returns list of bill_nos that reached zero."""
    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        raise FileNotFoundError(f"Missing vouchers file: {vouchers_file}")

    payment_map = {
        v["bill_no"]: Decimal(v["payment"])
        for v in vouchers
        if v.get("payment")
    }
    if not payment_map:
        return []

    rows = []
    completed_bill_nos = []
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
                    if new_balance == Decimal("0"):
                        completed_bill_nos.append(bill_no)
                except Exception:
                    pass
            rows.append(row)

    with vouchers_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return completed_bill_nos


def _archive_completed(bill_nos):
    """Move completed vouchers and their installments to completed_* CSV files."""
    if not bill_nos:
        return
    bill_set = set(bill_nos)

    # --- Archive vouchers ---
    vouchers_file = DATA_DIR / "vouchers.csv"
    completed_vouchers_file = DATA_DIR / "completed_vouchers.csv"
    remaining_vouchers = []
    completed_vouchers = []
    v_fieldnames = None
    if vouchers_file.exists():
        with vouchers_file.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            v_fieldnames = reader.fieldnames
            for row in reader:
                if row.get("bill_no", "").strip() in bill_set:
                    completed_vouchers.append(row)
                else:
                    remaining_vouchers.append(row)
        with vouchers_file.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=v_fieldnames)
            writer.writeheader()
            writer.writerows(remaining_vouchers)
        if completed_vouchers:
            write_header = not completed_vouchers_file.exists() or completed_vouchers_file.stat().st_size == 0
            with completed_vouchers_file.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=v_fieldnames)
                if write_header:
                    writer.writeheader()
                writer.writerows(completed_vouchers)

    # --- Archive installments ---
    inst_file = DATA_DIR / "installments.csv"
    completed_inst_file = DATA_DIR / "completed_installments.csv"
    remaining_inst = []
    completed_inst = []
    i_fieldnames = None
    if inst_file.exists():
        with inst_file.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            i_fieldnames = reader.fieldnames
            for row in reader:
                if row.get("bill_no", "").strip() in bill_set:
                    completed_inst.append(row)
                else:
                    remaining_inst.append(row)
        with inst_file.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=i_fieldnames)
            writer.writeheader()
            writer.writerows(remaining_inst)
        if completed_inst and i_fieldnames:
            write_header = not completed_inst_file.exists() or completed_inst_file.stat().st_size == 0
            with completed_inst_file.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=i_fieldnames)
                if write_header:
                    writer.writeheader()
                writer.writerows(completed_inst)


def _build_print_column(report_data, bal_width, coll_width):
    """Render one report as a list of _PRINT_COL_WIDTH-char padded strings."""
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
    col_hdr = f"{'Bill No':<7}  {'Balance':>{bal_width}}  {'Coll':>{coll_width}}"

    vouchers = report_data.get("vouchers", [])
    total_vouchers = len(vouchers)
    total_coll = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
    summary = f"Vouch:{total_vouchers}  Coll:{total_coll}"

    lines = [
        heading[:W].ljust(W),
        dash,
        col_hdr[:W].ljust(W),
        dash,
    ]
    for v in vouchers:
        bill = v["bill_no"][-7:]
        bal = v.get("balance", "")
        pay = v.get("payment", "") or ""
        row = f"{bill:<7}  {bal:>{bal_width}}  {pay:>{coll_width}}"
        lines.append(row[:W].ljust(W))
    lines.append(dash)
    lines.append(summary[:W].ljust(W))
    return lines


def write_print_collection_txt(output_path, reports_data):
    """Write up to 3 reports side by side in A4-optimized columns to a print TXT file."""
    if not reports_data:
        return

    all_vouchers = [v for r in reports_data for v in r.get("vouchers", [])]
    bal_width = max(
        len("Balance"),
        max((len(v.get("balance", "")) for v in all_vouchers), default=0),
    )
    coll_width = max(4, _PRINT_COL_WIDTH - 7 - 2 - bal_width - 2)

    columns = [_build_print_column(r, bal_width, coll_width) for r in reports_data]
    num_cols = len(columns)
    max_rows = max(len(c) for c in columns)

    for col in columns:
        while len(col) < max_rows:
            col.append(" " * _PRINT_COL_WIDTH)

    date_str = datetime.now().strftime("%Y-%m-%d")
    full_width = _PRINT_COL_WIDTH * num_cols + len(_PRINT_COL_SEP) * (num_cols - 1)
    eq_sep = "=" * full_width
    title_line = f"{'COLLECTION REPORT':<{full_width - 10}}{date_str:>10}"

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
