"""
Terminal UI layer for collection management.

All print() and input() calls live here.
No file paths, no CSV/JSON access, no imports from other coll_* modules.
"""

import os
from datetime import datetime
from decimal import Decimal, InvalidOperation


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def _sum_decimal_field(items, field, default="0"):
    return sum(Decimal(v.get(field, default) or default) for v in items)


def _format_decimal(d):
    if d == d.to_integral_value():
        return str(d.to_integral_value())
    return str(d.quantize(Decimal("0.01")))


def prompt_login():
    """Display login prompt and return (username, password), or None to exit."""
    clear_screen()
    print("\n" + "=" * 50)
    print("COLLECTION MANAGEMENT")
    print("=" * 50)
    print("  0. Exit")
    print("=" * 50)
    name = input("Username (0 to exit): ").strip()
    if name == "0":
        return None
    password = input("Password: ").strip()
    return name, password


def display_main_menu(menu_items, current_user):
    """Display the role-filtered main menu. menu_items is a list of label strings."""
    clear_screen()
    print("\n" + "=" * 50)
    print(f"COLLECTION MANAGEMENT  |  {current_user.name}  [{current_user.role}]")
    print("=" * 50)
    for i, label in enumerate(menu_items, start=1):
        print(f"{i}. {label}")
    print(f"{len(menu_items) + 1}. Exit")
    print("=" * 50)


def build_role_menu(current_user, permissions, action_registry):
    """Return list of (label, fn) for actions allowed to current_user's role."""
    allowed = permissions.get(current_user.role, frozenset())
    return [(label, fn) for key, label, fn in action_registry if key in allowed]


def get_menu_choice(max_choice=5):
    while True:
        try:
            choice = input(f"Enter your choice (1-{max_choice}): ").strip()
            choice_num = int(choice)
            if 1 <= choice_num <= max_choice:
                return choice_num
            print(f"Invalid choice: {choice_num}. Please enter 1-{max_choice}.")
        except ValueError:
            print(f"Invalid input: '{choice}'. Please enter a number.")


def get_reports_submenu_choice(current_role='distributor'):
    options = [("Salesman - pending collections", "salesman")]
    if current_role != 'salesman':
        options.append(("Beat - pending collections", "beat"))
    options += [
        ("Collections - Pending by age", "age"),
        ("Collections - Pending by amount", "amount"),
        ("Search voucher", "search"),
    ]
    clear_screen()
    print("\n" + "-" * 50)
    print("REPORTS")
    print("-" * 50)
    for i, (label, _) in enumerate(options, start=1):
        print(f"{i}. {label}")
    print("0. Back to main menu")
    print("-" * 50)
    while True:
        try:
            choice = input(f"Enter your choice (0-{len(options)}): ").strip()
            if choice.lower() == "b":
                return "back"
            n = int(choice)
            if n == 0:
                return "back"
            if 1 <= n <= len(options):
                return options[n - 1][1]
            print(f"Invalid choice: {n}. Please enter 0-{len(options)}.")
        except ValueError:
            print(f"Invalid input: '{choice}'. Please enter a number.")


def select_from_list(items, label, allow_multiple=False):
    """Display a numbered list and let the user choose one or more items."""
    print(f"\nSelect {label}:")
    for index, item in enumerate(items, start=1):
        print(f"  {index}. {item}")
    print("  b. Back")

    prompt = (
        f"Enter the number of the {label} (1-{len(items)})"
        + (" or comma-separated list/ranges (e.g. 1,3-5)" if allow_multiple else "")
        + ", or 'b' to go back: "
    )

    while True:
        choice = input(prompt).strip()
        if choice.lower() == "b":
            return None
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


def select_beat_with_summary(beats, summary, active_statuses=None, show_salesman_breakdown=False):
    """Display beats with voucher count and status. Only beats with no active report are numbered.

    active_statuses: dict[beat -> status_label] from load_active_beat_statuses().
    """
    if not beats:
        input("No beats found. Press Enter to go back: ")
        return None
    print("\nSelect a beat:\n")
    beat_width = max(len(b) for b in beats)
    selectable = []  # ordered list of beat names that get a number

    for beat in beats:
        info = summary.get(beat)
        status_label = active_statuses.get(beat) if active_statuses else None
        is_active = status_label is not None

        if info and info["total"] > 0:
            bal = info.get("balance_sum", 0)
            if is_active:
                print(f"   --  {beat:<{beat_width}}  {info['total']:>4} pending  bal: {bal}  [{status_label}]")
            else:
                selectable.append(beat)
                n = len(selectable)
                print(f"  {n:2}.  {beat:<{beat_width}}  {info['total']:>4} pending  bal: {bal}")
        else:
            print(f"   --  {beat:<{beat_width}}     no pending")

        if show_salesman_breakdown and info:
            by_sm = info.get("by_salesman", {})
            indent = " " * (8 + beat_width)
            for sm, count in sorted(by_sm.items()):
                print(f"{indent}{sm}: {count}")

    print("   b. Back")
    print()

    if not selectable:
        input("No beats available to start. Press Enter to go back: ")
        return None

    while True:
        choice = input(f"Enter the number of the beat (1-{len(selectable)}) or 'b': ").strip()
        if choice.lower() == "b":
            return None
        try:
            n = int(choice)
        except ValueError:
            print(f"Invalid input. Enter a number between 1 and {len(selectable)} or 'b'.")
            continue
        if 1 <= n <= len(selectable):
            return selectable[n - 1]
        print(f"Invalid selection. Enter a number between 1 and {len(selectable)} or 'b'.")


def select_salesman_with_counts(salesmen_counts):
    """Display salesmen with their pending voucher count. salesmen_counts: [(name, count), ...]. Return selected name or None."""
    print("\nSelect a salesman:\n")
    name_width = max(len(s) for s, _ in salesmen_counts)
    for idx, (name, count) in enumerate(salesmen_counts, start=1):
        print(f"  {idx:2}. {name:<{name_width}}  {count:>4} pending")
    print("   b. Back")
    print()
    while True:
        choice = input(f"Enter the number of the salesman (1-{len(salesmen_counts)}) or 'b': ").strip()
        if choice.lower() == "b":
            return None
        try:
            n = int(choice)
        except ValueError:
            print(f"Invalid input. Enter a number between 1 and {len(salesmen_counts)} or 'b'.")
            continue
        if 1 <= n <= len(salesmen_counts):
            return salesmen_counts[n - 1][0]
        print(f"Invalid selection. Enter a number between 1 and {len(salesmen_counts)} or 'b'.")


def paginate_text(text, page_size=20):
    lines = text.splitlines()
    total_pages = (len(lines) + page_size - 1) // page_size
    for page_num, start in enumerate(range(0, len(lines), page_size), start=1):
        for line in lines[start:start + page_size]:
            print(line)
        if page_num < total_pages:
            choice = input(f"--- page {page_num}/{total_pages} --- Enter to continue, 'b' to skip --- ").strip().lower()
            if choice == "b":
                return


def prompt_continue():
    while input("\nEnter 'b' to go back: ").strip().lower() != "b":
        pass


def prompt_report_selection(reports, labels=None, show_print=False):
    """Prompt user to select one report for editing.

    Returns None if user goes back, "PRINT" if user chose the print option,
    or [selected_path] for a normal selection.
    """
    print("\nSelect Collection List:")
    for index, report in enumerate(reports, start=1):
        label = labels[index - 1] if labels else report.name
        print(f"  {index}. {label}")
    if show_print:
        print("  P. Print reports")

    while True:
        choice = input(f"Enter report number (1-{len(reports)}, b to go back): ").strip()
        if choice.lower() == "b":
            return None
        if show_print and choice.lower() == "p":
            return "PRINT"
        try:
            selected = int(choice)
            if selected == 0:
                return None
            if 1 <= selected <= len(reports):
                return [reports[selected - 1]]
            print(f"Invalid selection: {selected}. Choose a number between 1 and {len(reports)}.")
        except ValueError:
            print(f"Invalid input: '{choice}'. Please enter a number.")


def display_report_with_focus(beats, salesmen, vouchers, current_idx):
    """Display full report with current record marked with >>>."""
    clear_screen()

    print("\n" + "=" * 80)
    print("COLLECTION LIST")
    print(f"Beats: {', '.join(beats)} | Salesmen: {', '.join(salesmen)}")
    print(f"Collection date: {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 80)

    if not vouchers:
        print("(No vouchers)")
        return

    bill_width = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    vdate_width = max(len("voucher_date"), max(len(v.get("voucher_date", "")) for v in vouchers))
    balance_width = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    payment_width = max(len("collection"), max(len(v.get("payment") or "") for v in vouchers))

    header = (
        f"   {'bill_no':<{bill_width}}  "
        f"{'voucher_date':<{vdate_width}}  "
        f"{'balance':>{balance_width}}  "
        f"{'collection':>{payment_width}}"
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
            f"{voucher.get('payment', ''):>{payment_width}}"
        )
        print(line)

    print(separator)
    total_balance = _sum_decimal_field(vouchers, "balance")
    total_payments = _sum_decimal_field(vouchers, "payment")
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
    print("  [n] = next record  [p] = previous record  [s] = skip  [q] = quit  [b] = back")

    default = current_payment if current_payment else _format_decimal(balance)
    prompt = f"Payment amount (or command) [{default}]: "

    while True:
        value = input(prompt).strip().lower()

        if value == "n":
            return current_payment, "next"
        elif value == "p":
            return current_payment, "prev"
        elif value == "s":
            return current_payment, "skip"
        elif value in ("q", "b"):
            return current_payment, "quit"
        elif value == "":
            return default, "next"
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
                return _format_decimal(payment), "next"
            except (ValueError, InvalidOperation):
                print(f"Invalid input. Enter a numeric amount or a command (n/p/s/q).")


# --- Report display functions ---

def display_salesman_beat_summary(salesman, grouped_by_beat):
    """Display beat-level summary for a salesman. Returns selected beat name or None."""
    today = datetime.now().strftime("%Y-%m-%d")
    clear_screen()
    print("\n" + "=" * 60)
    print("PENDING COLLECTIONS REPORT")
    print(f"Salesman : {salesman}")
    print(f"Date     : {today}")
    print("=" * 60)

    beats = sorted(grouped_by_beat)
    beat_stats = []
    grand_count = 0
    grand_balance = Decimal("0")

    for beat in beats:
        vouchers = grouped_by_beat[beat]
        balance = _sum_decimal_field(vouchers, "balance")
        beat_stats.append((beat, len(vouchers), balance))
        grand_count += len(vouchers)
        grand_balance += balance

    beat_w = max(len("beat"), max(len(b) for b in beats))
    cnt_w = max(len("vouchers"), max(len(str(c)) for _, c, _ in beat_stats))
    bal_w = max(len("balance"), max(len(str(b)) for _, _, b in beat_stats))

    header = f"  {'#':>3}  {'beat':<{beat_w}}  {'vouchers':>{cnt_w}}  {'balance':>{bal_w}}"
    sep = "-" * len(header)
    print(header)
    print(sep)
    for i, (beat, count, balance) in enumerate(beat_stats, start=1):
        print(f"  {i:>3}. {beat:<{beat_w}}  {count:>{cnt_w}}  {str(balance):>{bal_w}}")
    print(sep)
    print(f"  {'':>3}  {'TOTAL':<{beat_w}}  {grand_count:>{cnt_w}}  {str(grand_balance):>{bal_w}}")
    print("=" * 60)

    while True:
        choice = input(f"\nSelect beat (1-{len(beats)}) or 'b' to go back: ").strip().lower()
        if choice == "b":
            return None
        try:
            n = int(choice)
            if 1 <= n <= len(beats):
                return beats[n - 1]
            print(f"Invalid selection. Choose 1-{len(beats)}.")
        except ValueError:
            print("Invalid input. Enter a number or 'b'.")


def display_salesman_beat_vouchers(salesman, beat, vouchers):
    """Display all pending vouchers for a salesman in a specific beat."""
    today = datetime.now().strftime("%Y-%m-%d")
    clear_screen()
    print("\n" + "=" * 60)
    print("PENDING COLLECTIONS - BEAT DETAIL")
    print(f"Salesman : {salesman}")
    print(f"Beat     : {beat}")
    print(f"Date     : {today}")
    print("=" * 60)

    bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    date_w = max(len("date"), max(len(v["date"]) for v in vouchers))
    bal_w = max(len("balance"), max(len(v["balance"]) for v in vouchers))

    header = f"  {'bill_no':<{bill_w}}  {'date':<{date_w}}  {'balance':>{bal_w}}"
    sep = "-" * len(header)
    print(header)
    print(sep)
    for v in vouchers:
        print(f"  {v['bill_no']:<{bill_w}}  {v['date']:<{date_w}}  {v['balance']:>{bal_w}}")
    print(sep)
    total = _sum_decimal_field(vouchers, "balance")
    print(f"  Vouchers: {len(vouchers)}   Total balance: {total}")
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
        sm_balance = _sum_decimal_field(sm_vouchers, "balance")

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
    if not vouchers:
        print("No pending vouchers found.")
        return
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
    if not vouchers:
        print("No pending vouchers found.")
        return
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


def display_voucher_detail(voucher, installments, is_completed):
    """Display a voucher header and its full installment history."""
    status = "Completed" if is_completed else "Active"

    print("\n" + "=" * 60)
    print("VOUCHER DETAIL")
    print(f"Bill No  : {voucher['bill_no']}")
    print(f"Date     : {voucher['date']}")
    print(f"Amount   : {voucher['amount']}")
    print(f"Balance  : {voucher['balance']}")
    print(f"Beat     : {voucher['beat']}")
    print(f"Salesman : {voucher['salesman']}")
    print(f"Status   : {status}")
    print("=" * 60)

    if not installments:
        print("\nNo installment records found.")
        return

    print("\nInstallment History:")

    rows = []
    total = Decimal("0")
    for inst in installments:
        date = inst.get("date", "")
        amount_str = inst.get("amount", "")
        salesman = inst.get("salesman", "")
        try:
            amt = Decimal(amount_str)
        except (ValueError, InvalidOperation):
            amt = Decimal("0")
        total += amt
        rows.append((date, str(amt), salesman))

    rows.sort(key=lambda r: r[0], reverse=True)

    date_w = max(len("date"), max(len(r[0]) for r in rows))
    amt_w = max(len("amount"), max(len(r[1]) for r in rows))
    sm_w = max(len("salesman"), max(len(r[2]) for r in rows))

    header = f"  {'date':<{date_w}}  {'amount':>{amt_w}}  {'salesman':<{sm_w}}"
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)
    for date, amt, salesman in rows:
        print(f"  {date:<{date_w}}  {amt:>{amt_w}}  {salesman:<{sm_w}}")
    print(sep)
    print(f"  Total paid: {total}")
    print("=" * 60)


def display_confirm_stage_reports(reports, labels, stage_label):
    """Show a numbered list of reports awaiting confirmation. Returns chosen index or None."""
    print(f"\nSelect report to {stage_label}:")
    for i, label in enumerate(labels, start=1):
        print(f"  {i}. {label}")
    print("  b. Back")
    while True:
        choice = input(f"Enter report number (1-{len(labels)}, b to go back): ").strip()
        if choice.lower() == "b":
            return None
        try:
            n = int(choice)
            if 1 <= n <= len(labels):
                return n - 1
            print(f"Invalid selection. Choose 1-{len(labels)}.")
        except ValueError:
            print("Invalid input. Enter a number or 'b'.")


def display_report_for_review(report_data, txt_path):
    """Display a staging report's text file (or a fallback summary) for supervisor review."""
    if txt_path and txt_path.exists():
        print(txt_path.read_text(encoding="utf-8"))
    else:
        vouchers = report_data.get("vouchers", [])
        sel = report_data.get("selection", [])
        print(f"\n  Report: {', '.join(sel)}")
        print(f"  Vouchers: {len(vouchers)}")
        total_bal = _sum_decimal_field(vouchers, "balance")
        total_pay = _sum_decimal_field(vouchers, "payment")
        print(f"  Total balance: {total_bal}")
        if total_pay:
            print(f"  Total collected: {total_pay}")


def interactive_payment_editor(vouchers, beats, salesmen, start_idx=0):
    """Interactive payment editor with report display and navigation.

    Returns: (vouchers, True, None)           — all records visited
             (vouchers, False, current_idx)   — quit with save; caller saves installments + bookmark
             (None, False, None)              — quit without saving
    """
    current_idx = start_idx

    while current_idx < len(vouchers):
        total_payments = _sum_decimal_field(vouchers, "payment")
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
            confirm = input("\nDo you want to save changes? (y/n/b): ").strip().lower()
            if confirm == "y":
                return vouchers, False, current_idx
            elif confirm in ("n", "b"):
                return None, False, None

    # Redisplay with all payments filled in; current_idx == len(vouchers) so no >>> marker
    display_report_with_focus(beats, salesmen, vouchers, current_idx)
    return vouchers, True, None


# --- Add-vouchers UI ---

def prompt_csv_file_path(label):
    """Prompt for a CSV file path. Returns path string (may be empty to skip), or None to cancel."""
    value = input(f"\nPath to {label} CSV (Enter to skip, 'b' to cancel): ").strip()
    if value.lower() == "b":
        return None
    return value


def prompt_voucher_fields(salesmen=None):
    """Prompt for one voucher's fields. Returns dict of raw values, or None to finish/cancel.

    Beat is not asked here — it is set once at the session level by the caller.
    If salesmen is None, salesman is not asked (caller provides a fixed value).
    If salesmen is a list, a selection prompt is shown.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    print("\n-- New Voucher --")

    bill_no = input("Bill No ('b' or Enter when done): ").strip()
    if not bill_no or bill_no.lower() == "b":
        return None

    date_str = input(f"Date [YYYY-MM-DD, Enter for {today}]: ").strip() or today
    amount_str = input("Amount: ").strip()

    salesman = None
    if salesmen is not None:
        salesman = select_from_list(salesmen, "salesman")
        if salesman is None:
            return None

    return {"bill_no": bill_no, "date": date_str, "amount_str": amount_str, "salesman": salesman}


def prompt_installments_for_voucher(bill_no):
    """Prompt for installments in a tight loop. Amount first; 'b' or empty to stop.

    Salesman is not collected here — the caller sets it to the current user.
    """
    installments = []
    today = datetime.now().strftime("%Y-%m-%d")
    print("  Installments — enter amount, 'b' or empty to finish:")
    while True:
        amount_str = input("  Amount: ").strip()
        if not amount_str or amount_str.lower() == "b":
            break
        date_str = input(f"  Date [Enter for {today}]: ").strip() or today
        installments.append({"bill_no": bill_no, "date": date_str, "amount_str": amount_str})
    return installments


def display_addv_summary(vouchers, installments):
    """Print a summary table of vouchers and installments."""
    if not vouchers:
        print("  (No vouchers)")
        return

    bill_w = max(len("bill_no"), max(len(v["bill_no"]) for v in vouchers))
    date_w = max(len("date"), max(len(v["date"]) for v in vouchers))
    amt_w = max(len("amount"), max(len(v["amount"]) for v in vouchers))
    bal_w = max(len("balance"), max(len(v["balance"]) for v in vouchers))
    beat_w = max(len("beat"), max(len(v["beat"]) for v in vouchers))
    sm_w = max(len("salesman"), max(len(v["salesman"]) for v in vouchers))

    header = (
        f"  {'bill_no':<{bill_w}}  {'date':<{date_w}}"
        f"  {'amount':>{amt_w}}  {'balance':>{bal_w}}"
        f"  {'beat':<{beat_w}}  {'salesman':<{sm_w}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for v in vouchers:
        print(
            f"  {v['bill_no']:<{bill_w}}  {v['date']:<{date_w}}"
            f"  {v['amount']:>{amt_w}}  {v['balance']:>{bal_w}}"
            f"  {v['beat']:<{beat_w}}  {v['salesman']:<{sm_w}}"
        )
    print(sep)
    total_amount = sum(Decimal(v["amount"]) for v in vouchers)
    total_balance = sum(Decimal(v["balance"]) for v in vouchers)
    print(f"  Vouchers: {len(vouchers)}   Total amount: {total_amount}   Total balance: {total_balance}")

    if installments:
        ib_w = max(len("bill_no"), max(len(i["bill_no"]) for i in installments))
        id_w = max(len("date"), max(len(i["date"]) for i in installments))
        ia_w = max(len("amount"), max(len(i["amount"]) for i in installments))
        ism_w = max(len("salesman"), max(len(i["salesman"]) for i in installments))
        i_hdr = (
            f"    {'bill_no':<{ib_w}}  {'date':<{id_w}}"
            f"  {'amount':>{ia_w}}  {'salesman':<{ism_w}}"
        )
        i_sep = "-" * len(i_hdr)
        print(f"\n  Installments ({len(installments)}):")
        print(i_hdr)
        print(i_sep)
        for inst in installments:
            print(
                f"    {inst['bill_no']:<{ib_w}}  {inst['date']:<{id_w}}"
                f"  {inst['amount']:>{ia_w}}  {inst['salesman']:<{ism_w}}"
            )
        print(i_sep)
        total_inst = sum(Decimal(i["amount"]) for i in installments)
        print(f"    Total installments: {total_inst}")


def display_addv_report(report_data):
    """Display a staged add-vouchers report for review (confirm/finalize)."""
    mode = report_data.get("mode", "unknown")
    created_by = report_data.get("created_by", "")
    created_at = report_data.get("created_at", "")[:10]
    stages = report_data.get("stages", {})

    print(f"\nMode: {mode}  |  Created by: {created_by}  |  Date: {created_at}")
    print(
        f"Stages: Add={stages.get('add','')}  "
        f"Approve={stages.get('confirm','pending')}  "
        f"Post={stages.get('post','pending')}"
    )
    display_addv_summary(report_data.get("vouchers", []), report_data.get("installments", []))
