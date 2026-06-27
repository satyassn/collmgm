"""
File I/O and persistence layer for collection management.

Owns all path constants, CSV reads/writes, and JSON reads/writes.
No print() or input() calls. No imports from other coll_* modules.
"""

import binascii
import csv
import hashlib
import json
import os
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
    users_file = DATA_DIR / 'users.csv'
    if not users_file.exists():
        return None
    with users_file.open(newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('name', '').strip() != name:
                continue
            role = row.get('role', '').strip()
            stored = row.get('password_hash', '').strip()
            if role in ('salesman', 'supervisor', 'distributor') and _verify_password(stored, password):
                return User(name=name, role=role)
    return None


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
    """Reports submitted by salesman but not yet supervisor-confirmed (stages.submit == 'submitted')."""
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
    lines += [
        f"Beats: {', '.join(beats)}",
        f"Salesmen: {', '.join(salesmen)}",
        f"Collection date: {date_str}",
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
    total_balance = sum(Decimal(v.get("balance", "0") or "0") for v in vouchers)
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
    data = {v["bill_no"]: v["payment"] for v in vouchers if v.get("payment")}
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
        data.pop("__status__", None)  # discard legacy field if present
        return data, bookmark
    except Exception:
        return {}, None


def _append_installments_csv(vouchers):
    inst_file = DATA_DIR / "installments.csv"
    collection_date = datetime.now().strftime("%Y-%m-%d")
    created_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    existing_keys = set()
    if inst_file.exists():
        with inst_file.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                bn = row.get("bill_no", "").strip()
                dt = row.get("date", "").strip()
                if bn and dt:
                    existing_keys.add((bn, dt))

    rows_to_write = []
    for v in vouchers:
        payment = (v.get("payment") or "").strip()
        if not payment:
            continue
        try:
            amount = Decimal(payment)
        except (ValueError, InvalidOperation):
            print(f"Warning: skipping invalid payment for {v.get('bill_no')}: {payment!r}")
            continue
        if amount <= 0:
            continue
        key = (v["bill_no"], collection_date)
        if key in existing_keys:
            continue
        rows_to_write.append({
            "bill_no": v["bill_no"],
            "date": collection_date,
            "amount": payment,
            "salesman": v["salesman"],
            "created_by": "app",
            "created_at": created_at,
        })

    if not rows_to_write:
        return

    write_header = not inst_file.exists()
    with inst_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["bill_no", "date", "amount", "salesman", "created_by", "created_at"])
        if write_header:
            writer.writeheader()
        writer.writerows(rows_to_write)


def _update_vouchers_balance(vouchers):
    """Update balances in vouchers.csv atomically. Returns list of bill_nos that reached zero."""
    vouchers_file = DATA_DIR / "vouchers.csv"
    lock_file = DATA_DIR / ".vouchers.lock"
    if not vouchers_file.exists():
        raise FileNotFoundError(f"Missing vouchers file: {vouchers_file}")

    payment_map = {
        v["bill_no"]: Decimal(v["payment"])
        for v in vouchers
        if v.get("payment")
    }
    if not payment_map:
        return []

    try:
        lock_file.open('x').close()
    except FileExistsError:
        raise RuntimeError("vouchers.csv is locked by another process — please retry.")

    try:
        rows = []
        completed_bill_nos = []
        fieldnames = None
        with vouchers_file.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            if not fieldnames:
                raise ValueError(f"Cannot read columns from {vouchers_file} — file may be empty or malformed")
            for row in reader:
                bill_no = row.get("bill_no", "").strip()
                if bill_no in payment_map:
                    try:
                        old_balance = Decimal(row["balance"].strip())
                        new_balance = max(Decimal("0"), old_balance - payment_map[bill_no])
                        row["balance"] = str(new_balance.quantize(Decimal("0.01")))
                        if new_balance == Decimal("0"):
                            completed_bill_nos.append(bill_no)
                    except (ValueError, InvalidOperation):
                        print(f"Warning: bill_no {row.get('bill_no')} — balance '{row.get('balance')}' is invalid; skipping")
                rows.append(row)

        tmp_file = vouchers_file.with_suffix(".tmp")
        with tmp_file.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(str(tmp_file), str(vouchers_file))
    finally:
        lock_file.unlink(missing_ok=True)

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
    col_hdr = f"{'Bill No':<7} {'Balance':>{bal_width}} {'Coll':>{coll_width}}"

    vouchers = report_data.get("vouchers", [])
    total_vouchers = len(vouchers)
    total_bal = sum(Decimal(v.get("balance", "0") or "0") for v in vouchers)
    summary = f"#:{total_vouchers}  Bal:{total_bal}"

    lines = [
        heading[:W].ljust(W),
        col_hdr[:W].ljust(W),
        dash,
    ]
    for v in vouchers:
        bill = v["bill_no"][-7:]
        bal = v.get("balance", "")
        pay = v.get("payment", "") or ""
        row = f"{bill:<7} {bal:>{bal_width}} {pay:>{coll_width}}"
        lines.append(row[:W].ljust(W))
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


def _build_html_column(report_data):
    sel_type = report_data.get("selection_type", "beat")
    sel = report_data.get("selection", [])
    if sel_type == "beat_salesman" and len(sel) >= 2:
        heading = f"{sel[0]} / {sel[1]}"
    else:
        heading = ", ".join(sel)

    vouchers = report_data.get("vouchers", [])
    total_vouchers = len(vouchers)
    total_bal = sum(Decimal(v.get("balance", "0") or "0") for v in vouchers)
    total_coll = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)

    rows_html = "".join(
        f"<tr><td>{v['bill_no'][-7:]}</td>"
        f'<td class="sep">--</td>'
        f'<td class="num">{v.get("balance", "")}</td>'
        f'<td class="num">{v.get("payment", "") or ""}</td></tr>\n'
        for v in vouchers
    )
    coll_str = str(total_coll) if total_coll > 0 else ""

    return (
        f'<div class="col-heading">{heading}</div>\n'
        f"<table>\n"
        f"<colgroup><col style=\"width:10ch\"><col style=\"width:2ch\"><col style=\"width:10ch\"><col></colgroup>\n"
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
    """Write up to 3 reports side by side as a print-optimised HTML file."""
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
<title>Collection Report {date_str}</title>
<style>
  @page {{ margin: 10mm 8mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Courier New', Courier, monospace;
    font-size: 8pt;
    line-height: 1.05;
  }}
  .page-header {{
    display: flex;
    justify-content: space-between;
    font-weight: bold;
    font-size: 9pt;
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
    font-size: 7.5pt;
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
    padding: 0;
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
  <span>COLLECTION REPORT</span>
  <span>{date_str}</span>
</div>
<div class="columns">
{col_divs}
</div>
</body>
</html>"""

    with output_path.open("w", encoding="utf-8") as f:
        f.write(html)


# --- Add-vouchers pipeline helpers ---

def read_csv_file(path):
    """Read a CSV file and return (fieldnames, rows). Raises FileNotFoundError or ValueError."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with p.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        if not reader.fieldnames:
            raise ValueError(f"File appears empty or has no header: {path}")
        return list(reader.fieldnames), rows


def load_all_existing_bill_nos():
    """Return set of all bill_nos from vouchers.csv and completed_vouchers.csv."""
    bill_nos = set()
    for fname in ("vouchers.csv", "completed_vouchers.csv"):
        fpath = DATA_DIR / fname
        if not fpath.exists():
            continue
        with fpath.open(newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                b = row.get("bill_no", "").strip()
                if b:
                    bill_nos.add(b)
    return bill_nos


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
            if data.get("stages", {}).get("finalize") == "confirmed":
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
        if stages.get("confirm") == "confirmed" and stages.get("finalize") != "confirmed":
            result.append((path, data))
    return result


def write_new_vouchers(vouchers):
    """Append new vouchers to data/vouchers.csv."""
    fpath = DATA_DIR / "vouchers.csv"
    write_header = not fpath.exists() or fpath.stat().st_size == 0
    fieldnames = ["bill_no", "date", "amount", "balance", "beat", "salesman", "created_by", "created_at"]
    with fpath.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for v in vouchers:
            writer.writerow({k: v.get(k, "") for k in fieldnames})


def write_new_installments(installments):
    """Append new installments to data/installments.csv."""
    if not installments:
        return
    fpath = DATA_DIR / "installments.csv"
    write_header = not fpath.exists() or fpath.stat().st_size == 0
    fieldnames = ["bill_no", "date", "amount", "salesman", "created_by", "created_at"]
    with fpath.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for inst in installments:
            writer.writerow({k: inst.get(k, "") for k in fieldnames})
