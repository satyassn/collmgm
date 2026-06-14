#!/usr/bin/env python3
"""
Collection Menu - Main menu-driven program for collection operations.
==================================================
COLLECTION MANAGEMENT MENU
==================================================
1. coll-start - Generate list of voucher to start collection
2. coll-step2 - Enter collections for staged reports
3. coll-step3 - Review and submit reports
4. Reports
    4.1 - Salesman - pending collections
    4.2 - Beat - pending collections
    4.3 - Collections - Pending by age
    4.4 - Collections - Pending by amount
5. Exit
==================================================
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

NUMOF_TOP_AGED_VOUCHERS = 10
NUMOF_TOP_AMOUNT_VOUCHERS = 10


def display_menu():
    """Display the main menu options."""
    print("\n" + "=" * 50)
    print("COLLECTION MANAGEMENT MENU")
    print("=" * 50)
    print("1. coll-start  - Generate collection report")
    print("2. coll-step2  - Enter collections for staged reports")
    print("3. coll-step3  - Review and submit reports")
    print("4. Reports")
    print("   4.1 - Salesman - pending collections")
    print("   4.2 - Beat - pending collections")
    print("   4.3 - Collections - Pending by age")
    print("   4.4 - Collections - Pending by amount")
    print("5. Exit")
    print("=" * 50)


def get_menu_choice():
    """Get and validate user menu choice."""
    while True:
        try:
            choice = input("Enter your choice (1-5): ").strip()
            choice_num = int(choice)
            if choice_num in [1, 2, 3, 4, 5]:
                return choice_num
            print(f"Invalid choice: {choice_num}. Please enter 1-5.")
        except ValueError:
            print(f"Invalid input: '{choice}'. Please enter a number.")


def get_reports_submenu_choice():
    """Get and validate Reports sub-menu choice."""
    print("\n" + "-" * 50)
    print("REPORTS")
    print("-" * 50)
    print("1. Salesman - pending collections")
    print("2. Beat - pending collections")
    print("3. Collections - Pending by age")
    print("4. Collections - Pending by amount")
    print("0. Back to main menu")
    print("-" * 50)
    while True:
        try:
            choice = input("Enter your choice (0-4): ").strip()
            choice_num = int(choice)
            if choice_num in [0, 1, 2, 3, 4]:
                return choice_num
            print(f"Invalid choice: {choice_num}. Please enter 0-4.")
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


def _load_vouchers_by_criterion(selection_type, selection_values):
    """Load pending vouchers filtered by either beat or salesman (not both)."""
    vouchers = []
    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        raise FileNotFoundError(f"Missing vouchers file: {vouchers_file}")

    today = datetime.now().strftime("%Y-%m-%d")
    selected = set(selection_values)

    with vouchers_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_beat = row.get("beat", "").strip()
            row_salesman = row.get("salesman", "").strip()

            if selection_type == "beat" and row_beat not in selected:
                continue
            if selection_type == "salesman" and row_salesman not in selected:
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
    """Read collection data from a JSON file. Returns the vouchers list."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get("vouchers", [])


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
        if data.get("start_status") != "confirmed":
            continue
        if data.get("selection_type") != selection_type:
            continue
        if set(data.get("selection", [])) == selection_set:
            return path, data
    return None


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
    print(f"Total vouchers: {len(vouchers)} | Total due: {total_balance} | Total collection: {total_payments}")
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


def run_coll_start():
    """Execute coll_start by writing JSON and then generating a text report.
    - coll_start is about listing and verification of voucher going out for collection.
    - Vouchers are list for select beat or saleman. Only on criteria either beat or saleman not both.
    - Data should be maintained in json, status should be maintained in the json file. It is either verified or not. lets call this start_status. mark the initial status as new
    - Before generation of list, if a varified list exists for selected beat or saleman, prompt the user to use it or discard that before new list generation
    - At the end of report display prompt the use to verify the report. If use doesn't want to verify discard the report. If user confirms, mark start_start as confimed
    """
    print("\n" + "-" * 50)
    print("Executing: coll-start - Generate collection list")
    print("-" * 50)

    # Choose selection criterion: beat or salesman (not both)
    print("\nSelect vouchers by:")
    print("  1. Beat")
    print("  2. Salesman")
    while True:
        mode = input("Enter choice (1/2): ").strip()
        if mode in ("1", "2"):
            break
        print("Please enter 1 or 2.")
    selection_type = "beat" if mode == "1" else "salesman"

    try:
        items = load_beats() if selection_type == "beat" else load_salesmen()
        selection_values = select_from_list(items, f"{selection_type}(s)", allow_multiple=True)
    except Exception as error:
        print(f"Error: {error}")
        prompt_continue()
        return

    # Check for existing confirmed report for this selection before generating a new one
    existing = _find_confirmed_start_report(selection_type, selection_values)
    if existing:
        existing_path, existing_data = existing
        print(f"\nA confirmed list already exists for the selected {selection_type}: {existing_path.name}")
        while True:
            choice = input("Use existing (u) or discard and create new (n)? ").strip().lower()
            if choice == "u":
                txt_path = existing_path.with_suffix(".txt")
                print("\nExisting confirmed report:\n")
                if txt_path.exists():
                    print(txt_path.read_text(encoding="utf-8"))
                else:
                    print(f"  (Text report not found)  Vouchers: {len(existing_data['vouchers'])}")
                prompt_continue()
                return
            elif choice == "n":
                existing_path.unlink()
                txt = existing_path.with_suffix(".txt")
                if txt.exists():
                    txt.unlink()
                print("Existing report discarded. Generating new list...")
                break
            else:
                print("Please enter 'u' or 'n'.")

    # Load vouchers filtered by the single selected criterion
    try:
        vouchers = _load_vouchers_by_criterion(selection_type, selection_values)
    except Exception as error:
        print(f"Error loading vouchers: {error}")
        prompt_continue()
        return

    if not vouchers:
        print("\nNO Records Found\n")
        prompt_continue()
        return

    # Build file paths
    ensure_staging_dir()
    timestamp = datetime.now().strftime("%Y%m%d")
    safe_selection = "_".join(sanitize_filename_component(v) for v in selection_values)
    base_name = f"coll{timestamp}-{selection_type}-{safe_selection}"
    json_path = STAGING_DIR / f"{base_name}.json"
    txt_path = STAGING_DIR / f"{base_name}.txt"

    # Derive the complementary dimension for the text report header
    if selection_type == "beat":
        beats_for_report = selection_values
        salesmen_for_report = sorted({v["salesman"] for v in vouchers})
    else:
        beats_for_report = sorted({v["beat"] for v in vouchers})
        salesmen_for_report = selection_values

    start_data = {
        "start_status": "new",
        "selection_type": selection_type,
        "selection": selection_values,
        "vouchers": vouchers,
    }

    try:
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(start_data, f, indent=2)
        write_collection_text(txt_path, beats_for_report, salesmen_for_report, vouchers)
    except Exception as error:
        print(f"Failed to create report files: {error}")
        prompt_continue()
        return

    # Display the generated report
    print("\nGenerated report:\n")
    print(txt_path.read_text(encoding="utf-8"))

    # Prompt user to verify; discard files if not confirmed
    while True:
        confirm = input("Verify and confirm this report? (y/n): ").strip().lower()
        if confirm in ("y", "yes"):
            start_data["start_status"] = "confirmed"
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(start_data, f, indent=2)
            print("Report confirmed.")
            break
        elif confirm in ("n", "no"):
            json_path.unlink()
            if txt_path.exists():
                txt_path.unlink()
            print("Report discarded.")
            break
        else:
            print("Please enter 'y' or 'n'.")

    prompt_continue()


def run_coll_step3():
    """Execute coll-step3: Review and submit staged reports."""
    print("\n" + "-" * 50)
    print("Executing: coll-step3 - Review and submit reports")
    print("-" * 50)
    print("(Not yet implemented)")
    prompt_continue()


def run_report_salesman_pending():
    """Report 4.1: Salesman - pending collections.
    - Provide a list of saleman for selection
    - For the select salesman, display pending vouchers grouped by beat. Print a printer friendly and readable report.
    """
    print("\n" + "-" * 50)
    print("Report: Salesman - Pending Collections")
    print("-" * 50)

    try:
        salesmen = load_salesmen()
    except Exception as error:
        print(f"Error: {error}")
        prompt_continue()
        return

    salesman = select_from_list(salesmen, "salesman")

    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        print(f"Missing vouchers file: {vouchers_file}")
        prompt_continue()
        return

    grouped = {}
    with vouchers_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
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

    if not grouped:
        print(f"\nNo pending vouchers found for salesman: {salesman}")
        prompt_continue()
        return

    today = datetime.now().strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print("PENDING COLLECTIONS REPORT")
    print(f"Salesman : {salesman}")
    print(f"Date     : {today}")
    print("=" * 60)

    grand_count = 0
    grand_balance = Decimal("0")
    beat_stats = []  # (beat, count, balance) accumulated for summary

    for beat in sorted(grouped):
        beat_vouchers = grouped[beat]
        beat_balance = sum(Decimal(v["balance"]) for v in beat_vouchers)

        bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in beat_vouchers))
        date_w = max(len("date"), max(len(v["date"]) for v in beat_vouchers))
        bal_w = max(len("balance"), max(len(v["balance"]) for v in beat_vouchers))

        header = f"  {'bill_no':<{bill_w}}  {'date':<{date_w}}  {'balance':>{bal_w}}"
        sep = "-" * len(header)

        print(f"\nBeat: {beat}")
        print(sep)
        print(header)
        print(sep)
        for v in beat_vouchers:
            print(f"  {v['bill_no']:<{bill_w}}  {v['date']:<{date_w}}  {v['balance']:>{bal_w}}")
        print(sep)
        print(f"  Vouchers: {len(beat_vouchers)}   Beat total: {beat_balance}")

        grand_count += len(beat_vouchers)
        grand_balance += beat_balance
        beat_stats.append((beat, len(beat_vouchers), beat_balance))

    # Summary — repeat beat stats so they're visible without scrolling
    beat_w = max(len("beat"), max(len(b) for b, _, _ in beat_stats))
    cnt_w = max(len("vouchers"), max(len(str(c)) for _, c, _ in beat_stats))
    bal_w = max(len("balance"), max(len(str(b)) for _, _, b in beat_stats))

    sum_header = f"  {'beat':<{beat_w}}  {'vouchers':>{cnt_w}}  {'balance':>{bal_w}}"
    sum_sep = "-" * len(sum_header)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print(sum_sep)
    print(sum_header)
    print(sum_sep)
    for beat, count, balance in beat_stats:
        print(f"  {beat:<{beat_w}}  {count:>{cnt_w}}  {str(balance):>{bal_w}}")
    print(sum_sep)
    print(f"  {'TOTAL':<{beat_w}}  {grand_count:>{cnt_w}}  {str(grand_balance):>{bal_w}}")
    print("=" * 60)

    prompt_continue()


def run_report_beat_pending():
    """Report 4.2: Beat - pending collections.
    - List available beats and let user select one to view pending collections.
    - Show pending collections for the selected beat. Print a printer friendly and readable report.
    """
    print("\n" + "-" * 50)
    print("Report: Beat - Pending Collections")
    print("-" * 50)

    try:
        beats = load_beats()
    except Exception as error:
        print(f"Error: {error}")
        prompt_continue()
        return

    beat = select_from_list(beats, "beat")

    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        print(f"Missing vouchers file: {vouchers_file}")
        prompt_continue()
        return

    grouped = {}  # salesman -> list of vouchers
    with vouchers_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
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

    if not grouped:
        print(f"\nNo pending vouchers found for beat: {beat}")
        prompt_continue()
        return

    today = datetime.now().strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print("PENDING COLLECTIONS REPORT")
    print(f"Beat     : {beat}")
    print(f"Date     : {today}")
    print("=" * 60)

    grand_count = 0
    grand_balance = Decimal("0")
    salesman_stats = []  # (salesman, count, balance) for summary

    for salesman in sorted(grouped):
        salesman_vouchers = grouped[salesman]
        salesman_balance = sum(Decimal(v["balance"]) for v in salesman_vouchers)

        bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in salesman_vouchers))
        date_w = max(len("date"), max(len(v["date"]) for v in salesman_vouchers))
        bal_w = max(len("balance"), max(len(v["balance"]) for v in salesman_vouchers))

        header = f"  {'bill_no':<{bill_w}}  {'date':<{date_w}}  {'balance':>{bal_w}}"
        sep = "-" * len(header)

        print(f"\nSalesman: {salesman}")
        print(sep)
        print(header)
        print(sep)
        for v in salesman_vouchers:
            print(f"  {v['bill_no']:<{bill_w}}  {v['date']:<{date_w}}  {v['balance']:>{bal_w}}")
        print(sep)
        print(f"  Vouchers: {len(salesman_vouchers)}   Salesman total: {salesman_balance}")

        grand_count += len(salesman_vouchers)
        grand_balance += salesman_balance
        salesman_stats.append((salesman, len(salesman_vouchers), salesman_balance))

    # Summary — repeat salesman stats so they're visible without scrolling
    sm_w = max(len("salesman"), max(len(s) for s, _, _ in salesman_stats))
    cnt_w = max(len("vouchers"), max(len(str(c)) for _, c, _ in salesman_stats))
    bal_w = max(len("balance"), max(len(str(b)) for _, _, b in salesman_stats))

    sum_header = f"  {'salesman':<{sm_w}}  {'vouchers':>{cnt_w}}  {'balance':>{bal_w}}"
    sum_sep = "-" * len(sum_header)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print(sum_sep)
    print(sum_header)
    print(sum_sep)
    for salesman, count, balance in salesman_stats:
        print(f"  {salesman:<{sm_w}}  {count:>{cnt_w}}  {str(balance):>{bal_w}}")
    print(sum_sep)
    print(f"  {'TOTAL':<{sm_w}}  {grand_count:>{cnt_w}}  {str(grand_balance):>{bal_w}}")
    print("=" * 60)

    prompt_continue()


def run_report_collections_by_age():
    """Report 4.3: Collections - Pending by age.
    - Display the top N aged vouchers. N = NUMOF_TOP_AGED_VOUCHERS
    """
    print("\n" + "-" * 50)
    print("Report: Collections - Pending by Age")
    print("-" * 50)

    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        print(f"Missing vouchers file: {vouchers_file}")
        prompt_continue()
        return

    pending = []
    with vouchers_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                balance = Decimal(row.get("balance", "0").strip())
            except Exception:
                continue
            if balance <= 0:
                continue
            pending.append({
                "bill_no": row.get("bill_no", "").strip(),
                "date": row.get("date", "").strip(),
                "balance": str(balance),
                "beat": row.get("beat", "").strip(),
                "salesman": row.get("salesman", "").strip(),
            })

    if not pending:
        print("\nNo pending vouchers found.")
        prompt_continue()
        return

    # Oldest first (date string sorts correctly in YYYY-MM-DD format)
    pending.sort(key=lambda v: v["date"])
    top = pending[:NUMOF_TOP_AGED_VOUCHERS]

    today_date = datetime.now().date()
    today = today_date.strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print(f"TOP {NUMOF_TOP_AGED_VOUCHERS} AGED PENDING VOUCHERS")
    print(f"Date: {today}   Total pending: {len(pending)}")
    print("=" * 60)

    # Compute age in days for each row
    for v in top:
        try:
            v["age"] = (today_date - datetime.strptime(v["date"], "%Y-%m-%d").date()).days
        except Exception:
            v["age"] = 0

    bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in top))
    date_w = max(len("date"), max(len(v["date"]) for v in top))
    bal_w = max(len("balance"), max(len(v["balance"]) for v in top))
    beat_w = max(len("beat"), max(len(v["beat"]) for v in top))
    sm_w = max(len("salesman"), max(len(v["salesman"]) for v in top))
    age_w = max(len("age(days)"), max(len(str(v["age"])) for v in top))

    header = (
        f"  {'#':>3}  {'bill_no':<{bill_w}}  {'date':<{date_w}}"
        f"  {'balance':>{bal_w}}  {'beat':<{beat_w}}  {'salesman':<{sm_w}}  {'age(days)':>{age_w}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for rank, v in enumerate(top, start=1):
        print(
            f"  {rank:>3}  {v['bill_no']:<{bill_w}}  {v['date']:<{date_w}}"
            f"  {v['balance']:>{bal_w}}  {v['beat']:<{beat_w}}  {v['salesman']:<{sm_w}}  {v['age']:>{age_w}}"
        )
    print(sep)
    print("=" * 60)

    prompt_continue()


def run_report_collections_by_amount():
    """Report 4.4: Collections - Pending by amount.
    - Display the top N amount vouchers. N = NUMOF_TOP_AMOUNT_VOUCHERS
    """
    print("\n" + "-" * 50)
    print("Report: Collections - Pending by Amount")
    print("-" * 50)

    vouchers_file = DATA_DIR / "vouchers.csv"
    if not vouchers_file.exists():
        print(f"Missing vouchers file: {vouchers_file}")
        prompt_continue()
        return

    pending = []
    with vouchers_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
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

    if not pending:
        print("\nNo pending vouchers found.")
        prompt_continue()
        return

    # Largest balance first
    pending.sort(key=lambda v: v["balance"], reverse=True)
    top = pending[:NUMOF_TOP_AMOUNT_VOUCHERS]

    today = datetime.now().strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print(f"TOP {NUMOF_TOP_AMOUNT_VOUCHERS} PENDING VOUCHERS BY AMOUNT")
    print(f"Date: {today}   Total pending: {len(pending)}")
    print("=" * 60)

    # Convert balance back to str for display
    top_display = [{**v, "balance": str(v["balance"])} for v in top]

    bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in top_display))
    bal_w = max(len("balance"), max(len(v["balance"]) for v in top_display))
    date_w = max(len("date"), max(len(v["date"]) for v in top_display))
    beat_w = max(len("beat"), max(len(v["beat"]) for v in top_display))
    sm_w = max(len("salesman"), max(len(v["salesman"]) for v in top_display))

    header = (
        f"  {'#':>3}  {'bill_no':<{bill_w}}  {'balance':>{bal_w}}"
        f"  {'date':<{date_w}}  {'beat':<{beat_w}}  {'salesman':<{sm_w}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for rank, v in enumerate(top_display, start=1):
        print(
            f"  {rank:>3}  {v['bill_no']:<{bill_w}}  {v['balance']:>{bal_w}}"
            f"  {v['date']:<{date_w}}  {v['beat']:<{beat_w}}  {v['salesman']:<{sm_w}}"
        )
    print(sep)
    print("=" * 60)

    prompt_continue()


def run_reports():
    """Reports sub-menu loop."""
    while True:
        choice = get_reports_submenu_choice()
        if choice == 0:
            return
        elif choice == 1:
            run_report_salesman_pending()
        elif choice == 2:
            run_report_beat_pending()
        elif choice == 3:
            run_report_collections_by_age()
        elif choice == 4:
            run_report_collections_by_amount()


def main():
    """Main menu loop."""
    while True:
        display_menu()
        choice = get_menu_choice()

        if choice == 1:
            run_coll_start()
        elif choice == 2:
            run_coll_step2()
        elif choice == 3:
            run_coll_step3()
        elif choice == 4:
            run_reports()
        elif choice == 5:
            print("\nExiting Collection Management Menu. Goodbye!\n")
            sys.exit(0)


if __name__ == "__main__":
    main()
