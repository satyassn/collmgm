#!/usr/bin/env python3
"""
Collection Menu - Main menu-driven program for collection operations.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STAGING_DIR = ROOT_DIR / "staging"


def display_menu():
    """Display the main menu options."""
    print("\n" + "=" * 50)
    print("COLLECTION MANAGEMENT MENU")
    print("=" * 50)
    print("1. coll-step1 - Generate collection report")
    print("2. coll-step2 - Enter payments for staged reports")
    print("3. Exit")
    print("=" * 50)


def get_menu_choice():
    """Get and validate user menu choice."""
    while True:
        try:
            choice = input("Enter your choice (1-3): ").strip()
            choice_num = int(choice)
            if choice_num in [1, 2, 3]:
                return choice_num
            print(f"Invalid choice: {choice_num}. Please enter 1, 2, or 3.")
        except ValueError:
            print(f"Invalid input: '{choice}'. Please enter a number.")


def load_beats():
    """Read beat names from beats.csv."""
    beats = []
    beats_file = DATA_DIR / "beats.csv"
    if not beats_file.exists():
        raise FileNotFoundError(f"Missing beats file: {beats_file}")

    with beats_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("name", "").strip()
            if name:
                beats.append(name)

    if not beats:
        raise ValueError("No beats found in data/beats.csv.")
    return beats


def load_salesmen():
    """Read salesman names from users.csv."""
    salesmen = []
    users_file = DATA_DIR / "users.csv"
    if not users_file.exists():
        raise FileNotFoundError(f"Missing users file: {users_file}")

    with users_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            role = row.get("role", "").strip().lower()
            name = row.get("name", "").strip()
            if role == "salesman" and name:
                salesmen.append(name)

    if not salesmen:
        raise ValueError("No salesmen found in data/users.csv.")
    return salesmen


def select_from_list(items, label, allow_multiple=False):
    """Display a numbered list and let the user choose one or more items."""
    print(f"\nSelect {label}:")
    for index, item in enumerate(items, start=1):
        print(f"  {index}. {item}")

    prompt = (
        f"Enter the number of the {label} (1-{len(items)})"
        + (" or comma-separated list/ranges (e.g. 1,3-5)" if allow_multiple else "")
        + ": "
    )

    while True:
        choice = input(prompt).strip()
        if allow_multiple and choice.lower() == "all":
            return list(items)

        selections = []
        for part in choice.split(","):
            part = part.strip()
            if not part:
                continue
            if allow_multiple and "-" in part:
                bounds = part.split("-")
                if len(bounds) != 2:
                    selections = None
                    break
                try:
                    start = int(bounds[0])
                    end = int(bounds[1])
                except ValueError:
                    selections = None
                    break
                if start > end:
                    selections = None
                    break
                selections.extend(range(start, end + 1))
            else:
                try:
                    selections.append(int(part))
                except ValueError:
                    selections = None
                    break

        if selections is None or not selections:
            print(f"Invalid input: '{choice}'. Please enter valid number(s).")
            continue

        normalized = sorted(set(selections))
        if any(num < 1 or num > len(items) for num in normalized):
            print(f"Invalid selection: '{choice}'. Choose valid number(s) between 1 and {len(items)}.")
            continue

        if allow_multiple:
            return [items[num - 1] for num in normalized]

        if len(normalized) != 1:
            print(f"Please select exactly one {label}.")
            continue

        return items[normalized[0] - 1]


def sanitize_filename_component(value):
    """Make a value safe for use in a filename."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())
    return safe or "unknown"


def load_vouchers_for_collection(beats, salesmen):
    """Load pending vouchers for the selected beats and salesmen."""
    vouchers = []
    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        raise FileNotFoundError(f"Missing vouchers file: {vouchers_file}")

    today = datetime.now().strftime("%Y-%m-%d")
    selected_beats = set(beats)
    selected_salesmen = set(salesmen)

    with vouchers_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_beat = row.get("beat", "").strip()
            row_salesman = row.get("salesman", "").strip()
            if row_beat not in selected_beats:
                continue
            if row_salesman not in selected_salesmen:
                continue

            balance_str = row.get("balance", "0").strip()
            try:
                balance = Decimal(balance_str)
            except Exception:
                continue

            if balance > 0:
                vouchers.append({
                    "bill_no": row.get("bill_no", "").strip(),
                    "date": today,
                    "balance": str(balance),
                    "payment": "",
                    "beat": row_beat,
                    "salesman": row_salesman,
                })
    return vouchers


def ensure_staging_dir():
    """Ensure that the staging directory exists."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)


def save_collection_json(path, vouchers):
    """Write the collection data to a JSON file."""
    with path.open("w", encoding="utf-8") as f:
        json.dump(vouchers, f, indent=2)


def load_collection_json(path):
    """Read collection data from a JSON file."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_collection_text(path, beats, salesmen, vouchers):
    """Write the text collection report from the JSON data.

    The report shows collection date (today) at the top and omits the per-row
    voucher date column from the details.
    """
    if not vouchers:
        raise ValueError("No vouchers available to write to text report.")

    date_str = datetime.now().strftime("%Y-%m-%d")
    bill_width = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    balance_width = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    payment_width = max(len("payment"), max(len(v.get("payment", "")) for v in vouchers))
    beat_width = max(len("beat"), max(len(v["beat"]) for v in vouchers))
    salesman_width = max(len("salesman"), max(len(v["salesman"]) for v in vouchers))

    header = (
        f"{ 'bill_no':<{bill_width}}  "
        f"{ 'balance':>{balance_width}}  "
        f"{ 'payment':>{payment_width}}  "
        f"{ 'beat':<{beat_width}}  "
        f"{ 'salesman':<{salesman_width}}"
    )
    separator = "-" * len(header)

    lines = [
        "COLLECTION REPORT",
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


def list_staging_reports():
    """List staged collection JSON reports."""
    if not STAGING_DIR.exists():
        return []
    reports = sorted(STAGING_DIR.glob("coll*.json"))
    return reports


def prompt_report_selection(reports):
    """Prompt user to select one report or all reports for editing."""
    print("\nSelect staged report to edit:")
    print("  0. All reports")
    for index, report in enumerate(reports, start=1):
        print(f"  {index}. {report.name}")

    while True:
        choice = input(f"Enter report number (0-{len(reports)}) or 'all': ").strip().lower()
        if choice == "all" or choice == "0":
            return reports
        try:
            selected = int(choice)
            if 1 <= selected <= len(reports):
                return [reports[selected - 1]]
            print(f"Invalid selection: {selected}. Choose a number between 0 and {len(reports)}.")
        except ValueError:
            print(f"Invalid input: '{choice}'. Please enter a number or 'all'.")


def display_report_with_focus(beats, salesmen, vouchers, current_idx):
    """Display full report with current record marked with >>>."""
    os.system("clear" if os.name == "posix" else "cls")
    
    print("\n" + "=" * 80)
    print("COLLECTION REPORT")
    print(f"Beats: {', '.join(beats)} | Salesmen: {', '.join(salesmen)}")
    print(f"Collection date: {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 80)
    
    if not vouchers:
        print("(No vouchers)")
        return
    
    bill_width = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    balance_width = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    payment_width = max(len("payment"), max(len(v.get("payment", "")) for v in vouchers) if vouchers else 1)
    beat_width = max(len("beat"), max(len(v["beat"]) for v in vouchers))
    salesman_width = max(len("salesman"), max(len(v["salesman"]) for v in vouchers))
    
    header = (
        f"   {'bill_no':<{bill_width}}  "
        f"{'balance':>{balance_width}}  "
        f"{'payment':>{payment_width}}  "
        f"{'beat':<{beat_width}}  "
        f"{'salesman':<{salesman_width}}"
    )
    separator = "-" * len(header)
    
    print(header)
    print(separator)
    
    for idx, voucher in enumerate(vouchers):
        marker = ">>>" if idx == current_idx else "   "
        line = (
            f"{marker} {voucher['bill_no']:<{bill_width}}  "
            f"{voucher['balance']:>{balance_width}}  "
            f"{voucher.get('payment', ''):>{payment_width}}  "
            f"{voucher['beat']:<{beat_width}}  "
            f"{voucher['salesman']:<{salesman_width}}"
        )
        print(line)
    
    print(separator)
    total_balance = sum(Decimal(v["balance"]) for v in vouchers)
    total_payments = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
    print(f"Total vouchers: {len(vouchers)} | Total due: {total_balance} | Total entered: {total_payments}")
    print("=" * 80)


def get_payment_input(voucher, current_idx, total_records, total_payments_so_far):
    """Prompt user for payment with navigation support.
    
    Returns tuple: (payment_amount_str, action) where action is 'next', 'prev', 'skip', 'quit'
    """
    balance = Decimal(voucher.get("balance", "0") or "0")
    current_payment = voucher.get("payment", "")
    
    print(f"\nRecord {current_idx + 1}/{total_records}")
    print(f"Bill: {voucher['bill_no']} | Balance: {balance}")
    print(f"Current payment: {current_payment or '(none)'} | Running total: {total_payments_so_far}")
    print("Controls: Enter amount, or press:")
    print("  [n] = next record  [p] = previous record  [s] = skip  [q] = quit")
    
    while True:
        value = input("Payment amount (or command): ").strip().lower()
        
        if value == "n":
            return current_payment, "next"
        elif value == "p":
            return current_payment, "prev"
        elif value == "s":
            return current_payment, "skip"
        elif value == "q":
            return current_payment, "quit"
        elif value == "":
            return current_payment, "next"
        else:
            try:
                payment = Decimal(value)
                if payment < 0:
                    print("Payment must be zero or positive.")
                    continue
                if payment > balance:
                    print(f"Payment cannot exceed balance ({balance}).")
                    continue
                payment = payment.quantize(Decimal('0.01'))
                return (str(payment) if payment != payment.to_integral() else str(payment.to_integral())), "next"
            except Exception:
                print(f"Invalid input. Enter a numeric amount or a command (n/p/s/q).")


def interactive_payment_editor(vouchers, beats, salesmen):
    """Interactive payment editor with report display and navigation.
    
    Returns: updated vouchers list with payment amounts, or None if aborted
    """
    current_idx = 0
    
    while current_idx < len(vouchers):
        total_payments = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
        display_report_with_focus(beats, salesmen, vouchers, current_idx)
        
        payment, action = get_payment_input(vouchers[current_idx], current_idx, len(vouchers), total_payments)
        vouchers[current_idx]["payment"] = payment
        
        if action == "next":
            current_idx += 1
        elif action == "prev":
            current_idx = max(0, current_idx - 1)
        elif action == "skip":
            current_idx += 1
        elif action == "quit":
            confirm = input("\nQuit without finishing? (y/n): ").strip().lower()
            if confirm == "y":
                return None  # Signal abort
            # else continue editing
    
    return vouchers


def run_coll_step2():
    """Execute coll-step2 by editing payments for staged reports."""
    reports = list_staging_reports()
    if not reports:
        print("\nNo staged reports found in staging/ to edit.")
        prompt_continue()
        return

    selected_reports = prompt_report_selection(reports)
    for report_path in selected_reports:
        print(f"\nEditing staged report: {report_path.name}")
        try:
            vouchers = load_collection_json(report_path)
        except Exception as error:
            print(f"Failed to read report {report_path.name}: {error}")
            continue
        if not vouchers:
            print(f"Report {report_path.name} contains no vouchers.")
            continue

        beats = sorted({v['beat'] for v in vouchers})
        salesmen = sorted({v['salesman'] for v in vouchers})
        
        vouchers = interactive_payment_editor(vouchers, beats, salesmen)
        if vouchers is None:
            print(f"Payment entry cancelled for {report_path.name}.")
            continue
        
        total_vouchers = len(vouchers)
        total_collections = sum(Decimal(v.get('payment', '0') or '0') for v in vouchers)
        print("\nSummary:")
        print(f"  Total vouchers: {total_vouchers}")
        print(f"  Total collections: {total_collections}")

        confirm = input("Save changes to this report? (y/n): ").strip().lower()
        if confirm not in ['y', 'yes']:
            print(f"Discarded changes for {report_path.name}.")
            continue

        try:
            save_collection_json(report_path, vouchers)
            txt_path = report_path.with_suffix('.txt')
            write_collection_text(txt_path, beats, salesmen, vouchers)
            print(f"Saved JSON: {report_path}")
            print(f"Updated TXT: {txt_path}")
        except Exception as error:
            print(f"Failed to save updated report {report_path.name}: {error}")


def prompt_continue():
    """Pause until the user presses Enter."""
    input("\nPress Enter to continue to the main menu...")


def run_coll_step1():
    """Execute collection step 1 by writing JSON and then generating a text report."""
    print("\n" + "-" * 50)
    print("Executing: coll-step1 - Generate collection report")
    print("-" * 50)

    try:
        beats = load_beats()
        salesmen = load_salesmen()
        selected_beats = select_from_list(beats, "beat(s)", allow_multiple=True)
        selected_salesmen = select_from_list(salesmen, "salesman(s)", allow_multiple=True)
    except Exception as error:
        print(f"Error: {error}")
        prompt_continue()
        return

    vouchers = load_vouchers_for_collection(selected_beats, selected_salesmen)
    if not vouchers:
        print("\nNO Records Found\n")
        prompt_continue()
        return

    ensure_staging_dir()
    timestamp = datetime.now().strftime("%Y%m%d")
    safe_beats = "_".join(sanitize_filename_component(beat) for beat in selected_beats)
    safe_salesmen = "_".join(sanitize_filename_component(salesman) for salesman in selected_salesmen)
    base_name = f"coll{timestamp}-beats{len(selected_beats)}-{safe_beats}-salesmen{len(selected_salesmen)}-{safe_salesmen}"
    json_path = STAGING_DIR / f"{base_name}.json"
    txt_path = STAGING_DIR / f"{base_name}.txt"

    try:
        save_collection_json(json_path, vouchers)
        loaded_vouchers = load_collection_json(json_path)
        write_collection_text(txt_path, selected_beats, selected_salesmen, loaded_vouchers)
    except Exception as error:
        print(f"Failed to create report files: {error}")
        prompt_continue()
        return

    print("\nGenerated report:\n")
    report_text = txt_path.read_text(encoding="utf-8")
    print(report_text)
    prompt_continue()


def main():
    """Main menu loop."""
    while True:
        display_menu()
        choice = get_menu_choice()

        if choice == 1:
            run_coll_step1()
        elif choice == 2:
            run_coll_step2()
        elif choice == 3:
            print("\nExiting Collection Management Menu. Goodbye!\n")
            sys.exit(0)


if __name__ == "__main__":
    main()
