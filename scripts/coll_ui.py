"""
Terminal UI layer for collection management.

All print() and input() calls live here.
No file paths, no CSV/JSON access, no imports from other coll_* modules.
"""

import os
from datetime import datetime
from decimal import Decimal


def display_menu():
    print("\n" + "=" * 50)
    print("COLLECTION MANAGEMENT MENU")
    print("=" * 50)
    print("1. coll-start  - Generate collection report")
    print("2. coll-submit - Submit today collections")
    print("3. coll-finalize  - Review and submit reports")
    print("4. Reports")
    print("   4.1 - Salesman - pending collections")
    print("   4.2 - Beat - pending collections")
    print("   4.3 - Collections - Pending by age")
    print("   4.4 - Collections - Pending by amount")
    print("5. Exit")
    print("=" * 50)


def get_menu_choice():
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


def select_beat_with_summary(beats, summary):
    """Display beats with pending voucher counts and salesman breakdown, return selected beat name."""
    print("\nSelect a beat:\n")
    beat_width = max(len(b) for b in beats)
    for idx, beat in enumerate(beats, start=1):
        info = summary.get(beat)
        if info and info["total"] > 0:
            breakdown = ", ".join(f"{s}: {c}" for s, c in sorted(info["by_salesman"].items()))
            print(f"  {idx:2}. {beat:<{beat_width}}  {info['total']:>4} pending  [{breakdown}]")
        else:
            print(f"  {idx:2}. {beat:<{beat_width}}     no pending")
    print()
    while True:
        choice = input(f"Enter the number of the beat (1-{len(beats)}): ").strip()
        try:
            n = int(choice)
        except ValueError:
            print(f"Invalid input. Enter a number between 1 and {len(beats)}.")
            continue
        if 1 <= n <= len(beats):
            return beats[n - 1]
        print(f"Invalid selection. Enter a number between 1 and {len(beats)}.")


def prompt_continue():
    input("\nPress Enter to continue to the main menu...")


def prompt_report_selection(reports):
    """Prompt user to select one report for editing."""
    print("\nSelect staged report to edit:")
    for index, report in enumerate(reports, start=1):
        print(f"  {index}. {report.name}")

    while True:
        choice = input(f"Enter report number (1-{len(reports)}): ").strip()
        try:
            selected = int(choice)
            if 1 <= selected <= len(reports):
                return [reports[selected - 1]]
            print(f"Invalid selection: {selected}. Choose a number between 1 and {len(reports)}.")
        except ValueError:
            print(f"Invalid input: '{choice}'. Please enter a number.")


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
    vdate_width = max(len("voucher_date"), max(len(v.get("voucher_date", "")) for v in vouchers))
    balance_width = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    payment_width = max(len("collection"), max(len(v.get("payment", "")) for v in vouchers))
    beat_width = max(len("beat"), max(len(v["beat"]) for v in vouchers))
    salesman_width = max(len("salesman"), max(len(v["salesman"]) for v in vouchers))

    header = (
        f"   {'bill_no':<{bill_width}}  "
        f"{'voucher_date':<{vdate_width}}  "
        f"{'balance':>{balance_width}}  "
        f"{'collection':>{payment_width}}  "
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
            f"{voucher.get('voucher_date', ''):<{vdate_width}}  "
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

    prompt = (
        f"Payment amount (or command) [{current_payment}]: "
        if current_payment
        else "Payment amount (or command): "
    )
    while True:
        value = input(prompt).strip().lower()

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


# --- Report display functions ---

def display_report_salesman_pending(salesman, grouped_by_beat):
    today = datetime.now().strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print("PENDING COLLECTIONS REPORT")
    print(f"Salesman : {salesman}")
    print(f"Date     : {today}")
    print("=" * 60)

    grand_count = 0
    grand_balance = Decimal("0")
    beat_stats = []

    for beat in sorted(grouped_by_beat):
        beat_vouchers = grouped_by_beat[beat]
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


def display_report_beat_pending(beat, grouped_by_salesman):
    today = datetime.now().strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print("PENDING COLLECTIONS REPORT")
    print(f"Beat     : {beat}")
    print(f"Date     : {today}")
    print("=" * 60)

    grand_count = 0
    grand_balance = Decimal("0")
    salesman_stats = []

    for salesman in sorted(grouped_by_salesman):
        sm_vouchers = grouped_by_salesman[salesman]
        sm_balance = sum(Decimal(v["balance"]) for v in sm_vouchers)

        bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in sm_vouchers))
        date_w = max(len("date"), max(len(v["date"]) for v in sm_vouchers))
        bal_w = max(len("balance"), max(len(v["balance"]) for v in sm_vouchers))

        header = f"  {'bill_no':<{bill_w}}  {'date':<{date_w}}  {'balance':>{bal_w}}"
        sep = "-" * len(header)

        print(f"\nSalesman: {salesman}")
        print(sep)
        print(header)
        print(sep)
        for v in sm_vouchers:
            print(f"  {v['bill_no']:<{bill_w}}  {v['date']:<{date_w}}  {v['balance']:>{bal_w}}")
        print(sep)
        print(f"  Vouchers: {len(sm_vouchers)}   Salesman total: {sm_balance}")

        grand_count += len(sm_vouchers)
        grand_balance += sm_balance
        salesman_stats.append((salesman, len(sm_vouchers), sm_balance))

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


def display_report_by_age(vouchers, total_pending, limit):
    today = datetime.now().strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print(f"TOP {limit} AGED PENDING VOUCHERS")
    print(f"Date: {today}   Total pending: {total_pending}")
    print("=" * 60)

    bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    date_w = max(len("date"), max(len(v["date"]) for v in vouchers))
    bal_w = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    beat_w = max(len("beat"), max(len(v["beat"]) for v in vouchers))
    sm_w = max(len("salesman"), max(len(v["salesman"]) for v in vouchers))
    age_w = max(len("age(days)"), max(len(str(v["age"])) for v in vouchers))

    header = (
        f"  {'#':>3}  {'bill_no':<{bill_w}}  {'date':<{date_w}}"
        f"  {'balance':>{bal_w}}  {'beat':<{beat_w}}  {'salesman':<{sm_w}}  {'age(days)':>{age_w}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for rank, v in enumerate(vouchers, start=1):
        print(
            f"  {rank:>3}  {v['bill_no']:<{bill_w}}  {v['date']:<{date_w}}"
            f"  {v['balance']:>{bal_w}}  {v['beat']:<{beat_w}}  {v['salesman']:<{sm_w}}  {v['age']:>{age_w}}"
        )
    print(sep)
    print("=" * 60)


def display_report_by_amount(vouchers, total_pending, limit):
    today = datetime.now().strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print(f"TOP {limit} PENDING VOUCHERS BY AMOUNT")
    print(f"Date: {today}   Total pending: {total_pending}")
    print("=" * 60)

    bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    bal_w = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    date_w = max(len("date"), max(len(v["date"]) for v in vouchers))
    beat_w = max(len("beat"), max(len(v["beat"]) for v in vouchers))
    sm_w = max(len("salesman"), max(len(v["salesman"]) for v in vouchers))

    header = (
        f"  {'#':>3}  {'bill_no':<{bill_w}}  {'balance':>{bal_w}}"
        f"  {'date':<{date_w}}  {'beat':<{beat_w}}  {'salesman':<{sm_w}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for rank, v in enumerate(vouchers, start=1):
        print(
            f"  {rank:>3}  {v['bill_no']:<{bill_w}}  {v['balance']:>{bal_w}}"
            f"  {v['date']:<{date_w}}  {v['beat']:<{beat_w}}  {v['salesman']:<{sm_w}}"
        )
    print(sep)
    print("=" * 60)


def interactive_payment_editor(vouchers, beats, salesmen, start_idx=0):
    """Interactive payment editor with report display and navigation.

    Returns: (vouchers, True, None)           — all records visited
             (vouchers, False, current_idx)   — quit with save; caller saves installments + bookmark
             (None, False, None)              — quit without saving
    """
    current_idx = start_idx

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
            confirm = input("\nDo you want to save changes? (y/n): ").strip().lower()
            if confirm == "y":
                return vouchers, False, current_idx
            elif confirm == "n":
                return None, False, None

    return vouchers, True, None
