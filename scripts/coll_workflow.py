"""
Workflow coordinator layer for collection management.

Orchestrates UI ↔ data ↔ store interactions.
No direct print/input calls (delegates to coll_cli).
No direct file I/O (delegates to coll_store).
No direct CSV reads (delegates to coll_data).
Stage-transition rules shared with coll_api.py live in coll_orchestrate.py.
"""

import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation

from coll_orchestrate import (
    prepare_submit_review, apply_submit_approval,
    ActiveReportState, check_active_beat_report, generate_collection_list, apply_start_approval,
    compute_payment_dates, record_submit_payments,
    post_confirmed_report, return_post_stage,
)
from coll_store import (
    STAGING_DIR, PRINTS_DIR,
    ensure_staging_dir, ensure_prints_dir, save_report_json,
    write_collection_text, write_print_collection_txt, write_print_collection_html,
    sanitize_filename_component,
    _load_installments,
    archive_files, cancel_staging_report,
    read_finalize_checkpoint,
    verify_user, _load_pending_start_reports, _load_pending_submit_reports,
    read_csv_file, load_all_existing_bill_nos, load_addv_staged_bill_nos,
    load_addv_pending_confirm, load_addv_pending_finalize,
    write_new_vouchers, write_new_installments,
    bill_no_sort_key,
)
from coll_data import (
    load_beats, load_salesmen, load_beats_pending_summary, _load_vouchers_by_criterion,
    _load_confirmed_start_reports, load_active_beat_statuses,
    _load_submit_confirmed_reports,
    query_pending_by_salesman, query_pending_by_beat,
    query_pending_by_age, query_pending_by_amount,
    NUMOF_TOP_AGED_VOUCHERS, NUMOF_TOP_AMOUNT_VOUCHERS,
    search_voucher,
    validate_single_voucher, validate_addv_batch,
    load_addv_pending_confirm_by_beat,
)
from coll_cli import (
    read_input, prompt_login, clear_screen,
    select_from_list, select_beat_with_summary, select_salesman_with_counts,
    prompt_continue, prompt_report_selection,
    interactive_payment_editor,
    display_confirm_stage_reports, display_report_for_review,
    display_salesman_beat_summary, display_salesman_beat_vouchers,
    display_report_beat_pending,
    display_report_by_age, display_report_by_amount,
    display_voucher_detail,
    prompt_csv_file_path, prompt_voucher_fields, prompt_installments_for_voucher,
    display_addv_summary, display_addv_report, prompt_pending_addv_choice,
)


def run_login():
    """Prompt for credentials until valid. Returns authenticated User."""
    while True:
        result = prompt_login()
        if result is None:
            print("\nGoodbye!\n")
            sys.exit(0)
        name, password = result
        user = verify_user(name, password)
        if user:
            return user
        print("\nInvalid username or password. Please try again.")
        read_input("Press Enter to retry...")


def _report_label(report_data, fallback_name):
    """Return a human-readable label for a staging report."""
    sel_type = report_data.get("selection_type", "beat")
    sel = report_data.get("selection", [])
    if sel_type == "beat_salesman" and len(sel) >= 2:
        return f"Beat: {sel[0]} | Salesman: {sel[1]}"
    return fallback_name


def _build_report_map_and_labels(reports):
    report_map = {p: d for p, d in reports}
    report_paths = list(report_map.keys())
    labels = [_report_label(report_map[p], p.name) for p in report_paths]
    return report_map, report_paths, labels


def run_coll_start(current_user):
    try:
        beats = load_beats(current_user)
        summary = load_beats_pending_summary(current_user)
    except Exception as error:
        print(f"Error: {error}")
        prompt_continue()
        return

    active_statuses = load_active_beat_statuses()
    while True:
        clear_screen()
        print("\n" + "-" * 50)
        print("Generate Collection List")
        print("-" * 50)

        show_breakdown = current_user.role in ('supervisor', 'distributor')
        beat = select_beat_with_summary(beats, summary, active_statuses=active_statuses,
                                        show_salesman_breakdown=show_breakdown)
        if beat is None:
            return  # 'b' at beat selection → exit to main menu

        try:
            beat_vouchers = _load_vouchers_by_criterion("beat", [beat])
        except Exception as error:
            print(f"Error loading vouchers: {error}")
            prompt_continue()
            return

        if not beat_vouchers:
            print("\nNO Records Found\n")
            prompt_continue()
            return

        # Nested salesman selection — filter by current_user for salesman role
        if current_user.role == 'salesman':
            salesmen_in_beat = [current_user.name] if current_user.name in {v["salesman"] for v in beat_vouchers} else []
        else:
            salesmen_in_beat = sorted({v["salesman"] for v in beat_vouchers})

        if not salesmen_in_beat:
            print("\nNo vouchers found for your assigned salesman in this beat.")
            prompt_continue()
            return

        if len(salesmen_in_beat) > 1:
            sm_counts = [
                (sm, sum(1 for v in beat_vouchers if v["salesman"] == sm))
                for sm in salesmen_in_beat
            ]
            chosen_salesman = select_salesman_with_counts(sm_counts)
            if chosen_salesman is None:
                continue  # 'b' at salesman → back to beat selection
        else:
            chosen_salesman = salesmen_in_beat[0]

        selection_type = "beat_salesman"
        selection_values = [beat, chosen_salesman]

        # Filter in-memory to chosen salesman (no second CSV read)
        vouchers = [v for v in beat_vouchers if v["salesman"] == chosen_salesman]

        state, existing_path, existing_data = check_active_beat_report(selection_type, selection_values)
        if state != ActiveReportState.NONE:
            print(f"\nAn active collection already exists for Beat: {beat} | Salesman: {chosen_salesman}")

            if state != ActiveReportState.PENDING_START:
                if state == ActiveReportState.START_CONFIRMED:
                    print("This report has been approved by a supervisor. It cannot be modified.")
                else:
                    print("This report is in the submit/post pipeline. Complete it before starting a new collection.")
                prompt_continue()
                return

            # Report is in start stage — display it and let supervisor confirm or discard
            txt_path = existing_path.with_suffix(".txt")
            clear_screen()
            display_report_for_review(existing_data, txt_path)
            print("\n(Collection list is awaiting supervisor approval.)")

            while True:
                choice = read_input("\nApprove (y) / Cancel (c) / Back (b): ").strip().lower()
                if choice == "b":
                    break  # back to beat selection
                action = "approve" if choice == "y" else "cancel" if choice == "c" else None
                if action is None:
                    print("Please enter 'y', 'c', or 'b'.")
                    continue
                try:
                    apply_start_approval(existing_path, existing_data, action)
                    print(f"\nReport approved: {existing_path.name}" if action == "approve"
                          else "\nCollection list cancelled.")
                except Exception as error:
                    print(f"Failed to approve report: {error}")
                active_statuses = load_active_beat_statuses()
                prompt_continue()
                break

            continue  # back to beat selection

        outcome = generate_collection_list(beat, chosen_salesman, vouchers)
        if not outcome.ok:
            if outcome.reason == "lock_conflict":
                print(f"\nBeat '{beat}' was just claimed by another session. Please retry.")
                prompt_continue()
                active_statuses = load_active_beat_statuses()
                continue
            print(f"Failed to create report files: {outcome.error}")
            prompt_continue()
            return

        json_path, txt_path = outcome.json_path, outcome.txt_path

        clear_screen()
        print(txt_path.read_text(encoding="utf-8"))

        while True:
            confirm = read_input("Keep this collection list? (y/n): ").strip().lower()
            if confirm in ("y", "yes", "n", "no"):
                break
            print("Please enter 'y' or 'n'.")

        if confirm in ("n", "no"):
            cancel_staging_report(json_path, beat)
            active_statuses = load_active_beat_statuses()
            print("Collection list cancelled.")
            clear_screen()
            continue  # back to beat selection

        print("Collection list generated. Awaiting supervisor approval.")
        active_statuses = load_active_beat_statuses()
        break  # exit outer loop

    prompt_continue()


def _prompt_submit_for_review(report_path, report_data, vouchers, beats, salesmen):
    """Ask whether to submit a report for supervisor review and apply the result."""
    submit_confirm = read_input("Submit this report for supervisor review? (y/n/b): ").strip().lower()
    if submit_confirm not in ("y", "yes"):
        print(f"Collections saved. Submission deferred for {report_path.name}.")
        return
    try:
        record_submit_payments(report_path, report_data, vouchers, submit_for_review=True,
                               beats=beats, salesmen=salesmen)
        print(f"Submitted for supervisor review: {report_path.name}")
    except Exception as error:
        print(f"Failed to submit report {report_path.name}: {error}")


def run_coll_submit(current_user):
    all_confirmed = _load_confirmed_start_reports()
    # Salesmen can only see reports for their own beat+salesman combination
    if current_user.role == 'salesman':
        confirmed_reports = [
            (p, d) for p, d in all_confirmed
            if d.get('selection_type') == 'beat_salesman' and d.get('selection', [None, None])[1] == current_user.name
        ]
    else:
        confirmed_reports = all_confirmed
    if not confirmed_reports:
        clear_screen()
        print("\n" + "-" * 50)
        print("Submit Collections")
        print("-" * 50)
        print("\nNo approved collection lists found in staging/.")
        prompt_continue()
        return

    confirmed_map, report_paths, labels = _build_report_map_and_labels(confirmed_reports)

    while True:
        clear_screen()
        print("\n" + "-" * 50)
        print("Submit Collections")
        print("-" * 50)

        selected_paths = prompt_report_selection(report_paths, labels, show_print=True)
        if selected_paths is None:
            return
        if selected_paths != "PRINT":
            break

        # Print branch: multi-select up to 3 reports and generate print TXT
        chosen_labels = select_from_list(labels, "reports to print (up to 3)", allow_multiple=True)
        if chosen_labels is None:
            continue
        if len(chosen_labels) > 3:
            print("  Note: only the first 3 will be printed.")
            chosen_labels = chosen_labels[:3]
        chosen_reports = [confirmed_map[report_paths[labels.index(lbl)]] for lbl in chosen_labels]
        try:
            ensure_prints_dir()
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            out_path = PRINTS_DIR / f"print_{stamp}.html"
            write_print_collection_html(out_path, chosen_reports)
            print(f"\nPrint file saved: {out_path}")
            try:
                os.startfile(str(out_path))
            except Exception:
                pass
        except Exception as err:
            print(f"Failed to generate print file: {err}")
        # loop back to report selection

    for report_path in selected_paths:
        report_data = confirmed_map[report_path]
        if report_data.get("stages", {}).get("submit") == "submitted":
            txt_path = report_path.with_suffix(".txt")
            clear_screen()
            display_report_for_review(report_data, txt_path)
            print("\nThis report has been submitted for supervisor review. Editing is not allowed.")
            prompt_continue()
            continue
        if report_data.get("stages", {}).get("submit") == "returned":
            txt_path = report_path.with_suffix(".txt")
            clear_screen()
            display_report_for_review(report_data, txt_path)
            print("\nRETURN REQUESTED: This submission was returned by the supervisor for correction.")
            print("Please review the payment amounts and resubmit.")
            report_data.setdefault("stages", {})["submit"] = "inprogress"

        vouchers = sorted(report_data["vouchers"], key=lambda v: bill_no_sort_key(v["bill_no"]))
        selection_type = report_data.get("selection_type", "beat")
        selection = report_data.get("selection", [])

        if selection_type == "beat_salesman":
            beats = [selection[0]]
            salesmen = [selection[1]]
        elif selection_type == "beat":
            beats = selection
            salesmen = sorted({v["salesman"] for v in vouchers})
        else:
            beats = sorted({v["beat"] for v in vouchers})
            salesmen = selection

        installments, bookmark_bill_no = _load_installments(report_path)
        start_idx = 0
        if installments:
            for v in vouchers:
                entry = installments.get(v["bill_no"])
                if entry:
                    v["payment"] = entry.get("payment", "")
                    v["payment_date"] = entry.get("date", "")
            print(f"  Loaded {len(installments)} installment(s) from prior session.")
        if bookmark_bill_no:
            bill_nos = [v["bill_no"] for v in vouchers]
            if bookmark_bill_no in bill_nos:
                start_idx = bill_nos.index(bookmark_bill_no)
                print(f"  Resuming from bookmarked record: {bookmark_bill_no}")

        while True:
            pre_edit = read_input("\nEdit (e) / Cancel (c) / Back (b): ").strip().lower()
            if pre_edit in ("e", ""):
                break
            if pre_edit == "c":
                cancel_staging_report(report_path, selection[0] if selection else None)
                print("Collection list cancelled.")
                prompt_continue()
                break
            if pre_edit == "b":
                break
            print("Please enter 'e', 'c', or 'b'.")
        if pre_edit == "c":
            continue
        if pre_edit == "b":
            continue

        print(f"\nEditing report: {report_path.name}")
        vouchers, completed, quit_idx = interactive_payment_editor(vouchers, beats, salesmen, start_idx=start_idx)

        if vouchers is not None:
            compute_payment_dates(vouchers, installments)

        if not completed:
            if vouchers is not None:
                try:
                    bookmark = vouchers[quit_idx]["bill_no"] if quit_idx is not None else None
                    record_submit_payments(report_path, report_data, vouchers, submit_for_review=False,
                                           bookmark_bill_no=bookmark)
                    print(f"Progress saved as installments for {report_path.name}.")
                    if bookmark:
                        print(f"Bookmarked at: {bookmark}")
                    _prompt_submit_for_review(report_path, report_data, vouchers, beats, salesmen)
                except Exception as error:
                    print(f"Failed to save installments for {report_path.name}: {error}")
            else:
                print(f"Quit without saving for {report_path.name}.")
            continue

        while True:
            save_confirm = read_input("\nSave these collections? (y/n/b): ").strip().lower()
            if save_confirm in ("y", "yes", "n", "no", "b"):
                break
            print("Please enter 'y', 'n', or 'b'.")
        if save_confirm in ("n", "no", "b"):
            print(f"Changes not saved for {report_path.name}.")
            continue

        try:
            record_submit_payments(report_path, report_data, vouchers, submit_for_review=False)
        except Exception as error:
            print(f"Failed to save collections for {report_path.name}: {error}")
            continue

        _prompt_submit_for_review(report_path, report_data, vouchers, beats, salesmen)

    prompt_continue()


def run_coll_post(current_user):
    clear_screen()
    print("\n" + "-" * 50)
    print("Post Collections")
    print("-" * 50)

    stale = read_finalize_checkpoint()
    if stale:
        print(f"\n  WARNING: A previous post was interrupted at step {stale.get('step')}.")
        print(f"  Report: {stale.get('report')}")
        print("  Manual data verification is recommended before proceeding.\n")

    submit_reports = _load_submit_confirmed_reports()
    if not submit_reports:
        print("\nNo submitted reports ready for posting.")
        prompt_continue()
        return

    report_map, report_paths, labels = _build_report_map_and_labels(submit_reports)
    selected_paths = prompt_report_selection(report_paths, labels)
    if selected_paths is None:
        return

    for report_path in selected_paths:
        report_data = report_map[report_path]
        vouchers = sorted(report_data["vouchers"], key=lambda v: bill_no_sort_key(v["bill_no"]))
        report_data["vouchers"] = vouchers
        beat = report_data.get("selection", [None])[0]

        selection_type = report_data.get("selection_type", "beat")
        selection = report_data.get("selection", [])
        if selection_type == "beat_salesman":
            beats = [selection[0]]
            salesmen = [selection[1]]
        elif selection_type == "beat":
            beats = selection
            salesmen = sorted({v["salesman"] for v in vouchers})
        else:
            beats = sorted({v["beat"] for v in vouchers})
            salesmen = selection

        txt_path = report_path.with_suffix(".txt")
        try:
            write_collection_text(txt_path, beats, salesmen, vouchers,
                                  stage="post", status="pending")
        except Exception:
            pass

        print(f"\nReport: {report_path.name}")
        if txt_path.exists():
            print(txt_path.read_text(encoding="utf-8"))
        else:
            print(f"  (Text report not found. Vouchers: {len(vouchers)})")

        do_post = False
        while True:
            confirm = read_input("Post (y) / Return (r) / Back (b): ").strip().lower()
            if confirm in ("y", "yes"):
                do_post = True
                break
            if confirm == "r":
                try:
                    return_post_stage(report_path, report_data)
                    print("Returned to supervisor for re-approval.")
                except Exception as error:
                    print(f"Failed to return report: {error}")
                prompt_continue()
                break
            if confirm == "b":
                print(f"Posting skipped for {report_path.name}.")
                break
            print("Please enter 'y', 'r', or 'b'.")
        if not do_post:
            continue

        outcome = post_confirmed_report(report_path, posted_by=current_user.name)
        if not outcome.ok:
            step_note = f" (step {outcome.step_failed})" if outcome.step_failed else ""
            print(f"Failed to post {report_path.name}{step_note}: {outcome.error}")
            print("  IMPORTANT: A post checkpoint remains. Verify data before retrying.")
            continue
        if outcome.archive_warning:
            print(f"  Warning: could not archive {report_path.name}: {outcome.archive_warning}")

        print("\n" + "-" * 50)
        print(f"Posted: {report_path.name}")
        print(f"  Beat                 : {beat}")
        print(f"  Vouchers in report   : {len(vouchers)}")
        print(f"  Vouchers with payment: {outcome.paid_count}")
        print(f"  Total collected      : {outcome.total_collected}")
        print(f"  Fully settled        : {len(outcome.completed_bill_nos)}")
        print("-" * 50)

    prompt_continue()


def run_coll_approve_start(current_user):
    clear_screen()
    print("\n" + "-" * 50)
    print("Approve Collection List")
    print("-" * 50)

    while True:
        pending = _load_pending_start_reports()
        if not pending:
            print("\nNo reports awaiting approval.")
            prompt_continue()
            return

        report_map, report_paths, labels = _build_report_map_and_labels(pending)

        idx = display_confirm_stage_reports(report_paths, labels, "approve")
        if idx is None:
            return

        report_path = report_paths[idx]
        report_data = report_map[report_path]
        txt_path = report_path.with_suffix(".txt")

        clear_screen()
        display_report_for_review(report_data, txt_path)

        while True:
            confirm = read_input("\nApprove (y) / Return (r) / Cancel (c) / Back (b): ").strip().lower()
            if confirm == "b":
                break
            if confirm in ("r", "c"):
                action, done_msg = (("return", "Collection list returned — it must be regenerated by the salesman.")
                                    if confirm == "r" else ("cancel", "Collection list cancelled."))
                try:
                    apply_start_approval(report_path, report_data, action)
                    print(done_msg)
                except Exception as error:
                    print(f"Failed to {action} collection list: {error}")
                prompt_continue()
                return
            if confirm in ("y", "yes"):
                break
            print("Please enter 'y', 'r', 'c', or 'b'.")

        if confirm == "b":
            continue
        if confirm not in ("y", "yes"):
            continue

        try:
            apply_start_approval(report_path, report_data, "approve")
            print(f"Collection list approved: {report_path.name}")
        except Exception as error:
            print(f"Failed to approve report: {error}")
        prompt_continue()


def run_coll_approve_submit(current_user):
    clear_screen()
    print("\n" + "-" * 50)
    print("Approve Collections")
    print("-" * 50)

    while True:
        pending = _load_pending_submit_reports()
        if not pending:
            print("\nNo submitted collections awaiting approval.")
            prompt_continue()
            return

        report_map, report_paths, labels = _build_report_map_and_labels(pending)

        idx = display_confirm_stage_reports(report_paths, labels, "approve")
        if idx is None:
            return

        report_path = report_paths[idx]
        report_data = prepare_submit_review(report_path, report_map[report_path])
        txt_path = report_path.with_suffix(".txt")

        clear_screen()
        display_report_for_review(report_data, txt_path)

        while True:
            confirm = read_input("\nApprove (y) / Return (r) / Back (b): ").strip().lower()
            if confirm == "b":
                break
            action = "approve" if confirm in ("y", "yes") else "return" if confirm == "r" else None
            if action is None:
                print("Please enter 'y', 'r', or 'b'.")
                continue
            try:
                apply_submit_approval(report_path, report_data, action)
                if action == "approve":
                    print(f"Collections approved: {report_path.name}")
                else:
                    print("Collections returned to salesman for correction.")
            except Exception as error:
                verb = "approve" if action == "approve" else "return"
                print(f"Failed to {verb} collections: {error}")
            prompt_continue()
            return


def run_report_salesman_pending(current_user):
    if current_user.role == 'salesman':
        salesman = current_user.name
    else:
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
        if salesman is None:
            return

    grouped = query_pending_by_salesman(salesman)
    if not grouped:
        clear_screen()
        print(f"\nNo pending vouchers found for salesman: {salesman}")
        prompt_continue()
        return

    while True:
        beat = display_salesman_beat_summary(salesman, grouped)
        if beat is None:
            return
        display_salesman_beat_vouchers(salesman, beat, grouped[beat])
        while read_input("\nEnter 'b' to go back: ").strip().lower() != "b":
            pass



def run_report_beat_pending():
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
    if beat is None:
        return
    grouped = query_pending_by_beat(beat)
    if not grouped:
        print(f"\nNo pending vouchers found for beat: {beat}")
        prompt_continue()
        return
    display_report_beat_pending(beat, grouped)
    prompt_continue()


def run_report_collections_by_age(current_user=None):
    print("\n" + "-" * 50)
    print("Report: Collections - Pending by Age")
    print("-" * 50)
    top, total = query_pending_by_age(NUMOF_TOP_AGED_VOUCHERS, current_user)
    if not top:
        print("\nNo pending vouchers found.")
        prompt_continue()
        return
    display_report_by_age(top, total, NUMOF_TOP_AGED_VOUCHERS)
    prompt_continue()


def run_report_collections_by_amount(current_user=None):
    print("\n" + "-" * 50)
    print("Report: Collections - Pending by Amount")
    print("-" * 50)
    top, total = query_pending_by_amount(NUMOF_TOP_AMOUNT_VOUCHERS, current_user)
    if not top:
        print("\nNo pending vouchers found.")
        prompt_continue()
        return
    display_report_by_amount(top, total, NUMOF_TOP_AMOUNT_VOUCHERS)
    prompt_continue()


def run_voucher_search():
    print("\n" + "-" * 50)
    print("Report: Search Voucher")
    print("-" * 50)
    bill_no = read_input("Enter bill number: ").strip()
    if not bill_no:
        prompt_continue()
        return
    result = search_voucher(bill_no)
    if result is None:
        print(f"\nVoucher '{bill_no}' not found.")
    else:
        voucher, installments, is_completed = result
        display_voucher_detail(voucher, installments, is_completed)
    prompt_continue()


def run_reports(current_user):
    """Reports sub-menu loop."""
    from coll_cli import get_reports_submenu_choice
    while True:
        action = get_reports_submenu_choice(current_user.role)
        if action == "back":
            return
        elif action == "salesman":
            run_report_salesman_pending(current_user)
        elif action == "beat":
            run_report_beat_pending()
        elif action == "age":
            run_report_collections_by_age(current_user)
        elif action == "amount":
            run_report_collections_by_amount(current_user)
        elif action == "search":
            run_voucher_search()


def run_add_vouchers(current_user):
    """Entry point for Add Vouchers. Supervisor gets a sub-menu; others go straight to inline entry."""
    if current_user.role == 'supervisor':
        _run_add_vouchers_supervisor_menu(current_user)
    else:
        run_add_vouchers_inline(current_user)


def _run_add_vouchers_supervisor_menu(current_user):
    """Sub-menu shown to supervisors under Add Vouchers."""
    while True:
        clear_screen()
        print("\n" + "=" * 50)
        print("ADD VOUCHERS")
        print("=" * 50)
        print("1. Add new vouchers")
        print("2. Approve new vouchers")
        print("0. Back")
        print("=" * 50)
        choice = read_input("Enter choice (0-2): ").strip()
        if choice in ("0", "b", ""):
            return
        elif choice == "1":
            run_add_vouchers_inline(current_user)
        elif choice == "2":
            run_confirm_addv_by_beat(current_user)
        else:
            print("Invalid choice.")


def run_confirm_addv_by_beat(current_user):
    """Supervisor confirms pending new vouchers, reviewing by beat."""
    while True:
        clear_screen()
        print("\n" + "-" * 50)
        print("Approve New Vouchers")
        print("-" * 50)

        grouped = load_addv_pending_confirm_by_beat()
        if not grouped:
            print("\nNo new vouchers awaiting approval.")
            prompt_continue()
            return

        beats_sorted = sorted(grouped.keys())
        print("\nPending new vouchers by beat:")
        beat_w = max(len(b) for b in beats_sorted)
        for i, b in enumerate(beats_sorted, start=1):
            count = len(grouped[b]["vouchers"])
            print(f"  {i:2}. {b:<{beat_w}}   {count} voucher(s)")
        print("   b. Back")

        choice = read_input(f"\nSelect beat (1-{len(beats_sorted)}, b to go back): ").strip().lower()
        if choice == "b":
            return
        try:
            idx = int(choice) - 1
        except ValueError:
            continue
        if idx < 0 or idx >= len(beats_sorted):
            continue

        beat = beats_sorted[idx]
        beat_data = grouped[beat]

        clear_screen()
        print(f"\nBeat: {beat}  —  {len(beat_data['vouchers'])} voucher(s) pending approval")
        print("-" * 50)
        display_addv_summary(beat_data["vouchers"], beat_data["installments"])

        confirm = read_input("\nApprove these new vouchers? (y/n/b): ").strip().lower()
        if confirm == "b":
            continue
        if confirm not in ("y", "yes"):
            print("Skipped.")
            continue

        ok = 0
        for path, data in beat_data["files"]:
            data["stages"]["confirm"] = "confirmed"
            data["stage"] = "confirmed"
            try:
                save_report_json(path, data)
                ok += 1
            except Exception as e:
                print(f"  Error saving {path.name}: {e}")

        print(f"\nApproved: {len(beat_data['vouchers'])} voucher(s) for beat '{beat}'.")
        prompt_continue()
        return


def run_add_vouchers_inline(current_user):
    """Inline voucher entry — field by field, one or more vouchers per session.

    Beat is chosen once at the start and applies to all vouchers in the session.
    Salesman is auto-set for salesman role; supervisor/distributor pick per voucher.
    """
    try:
        beats = load_beats(current_user)
        salesmen = load_salesmen()
    except Exception as e:
        print(f"Error loading reference data: {e}")
        prompt_continue()
        return

    clear_screen()
    print("\n" + "-" * 50)
    print("Add Vouchers - Inline")
    print("-" * 50)

    # Check for pending batches created by this user
    my_pending = [(p, d) for p, d in load_addv_pending_confirm()
                  if d.get("created_by") == current_user.name]

    resume_path = None
    resume_data = None
    if my_pending:
        idx, action = prompt_pending_addv_choice(my_pending)
        if action == "back":
            return
        if action == "continue":
            resume_path, resume_data = my_pending[idx]

    # Beat selection — skipped when resuming (beat already fixed by existing batch)
    if resume_data:
        session_beat = resume_data["vouchers"][0]["beat"]
    else:
        session_beat = select_from_list(beats, "beat")
        if session_beat is None:
            return

    session_salesman = current_user.name if current_user.role == 'salesman' else None
    ask_salesman = session_salesman is None

    # Exclude resumed batch's own bill_nos from dedup check to avoid false conflicts
    resumed_bill_nos = {v["bill_no"] for v in resume_data["vouchers"]} if resume_data else set()
    existing_bill_nos = load_all_existing_bill_nos() | (load_addv_staged_bill_nos() - resumed_bill_nos)
    valid_beats = set(beats)
    valid_salesmen = set(salesmen)
    session_bill_nos = set()
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Prepopulate from resumed batch, or start fresh
    if resume_data:
        all_vouchers = list(resume_data["vouchers"])
        all_installments = list(resume_data["installments"])
        session_bill_nos = {v["bill_no"] for v in all_vouchers}
        clear_screen()
        print("\n" + "-" * 50)
        print("Add Vouchers - Inline")
        print(f"  Resuming batch from {resume_data['created_at'][:10]} — {len(all_vouchers)} voucher(s) loaded.")
        print("-" * 50)
        display_addv_summary(all_vouchers, all_installments)
        read_input("\nPress Enter to continue adding vouchers...")
    else:
        all_vouchers = []
        all_installments = []

    while True:
        clear_screen()
        print("\n" + "-" * 50)
        print("Add Vouchers - Inline")
        print(f"  Beat     : {session_beat}")
        if session_salesman:
            print(f"  Salesman : {session_salesman}")
        print(f"  Added    : {len(all_vouchers)} voucher(s) so far")
        if all_vouchers:
            last = all_vouchers[-1]
            print(f"  Last     : {last['bill_no']}  balance: {last['balance']}")
        print("-" * 50)

        fields = prompt_voucher_fields(salesmen=salesmen if ask_salesman else None)
        if fields is None:
            break

        salesman = fields["salesman"] if ask_salesman else session_salesman

        all_existing = existing_bill_nos | session_bill_nos
        errs, amount = validate_single_voucher(
            fields["bill_no"], fields["date"], fields["amount_str"],
            session_beat, salesman,
            all_existing, valid_beats, valid_salesmen,
        )
        if errs:
            for e in errs:
                print(f"  Error: {e}")
            read_input("\nPress Enter to continue...")
            continue

        inst_fields = prompt_installments_for_voucher(fields["bill_no"])

        inst_sum = Decimal("0")
        valid_insts = []
        for inst_f in inst_fields:
            try:
                ia = Decimal(inst_f["amount_str"])
            except InvalidOperation:
                print(f"  Warning: invalid installment amount '{inst_f['amount_str']}'; skipped.")
                continue
            if not ia.is_finite() or ia <= 0:
                print("  Warning: installment amount must be a positive number; skipped.")
                continue
            inst_sum += ia
            valid_insts.append(inst_f)

        if inst_sum > amount:
            print(f"  Error: total installments ({inst_sum}) exceed amount ({amount}). Voucher not added.")
            read_input("\nPress Enter to continue...")
            continue

        balance = (amount - inst_sum).quantize(Decimal("0.01"))
        bill_no = fields["bill_no"]

        all_vouchers.append({
            "bill_no": bill_no,
            "date": fields["date"],
            "amount": str(amount.quantize(Decimal("0.01"))),
            "balance": str(balance),
            "beat": session_beat,
            "salesman": salesman,
            "created_by": current_user.name,
            "created_at": now_str,
        })
        for inst_f in valid_insts:
            all_installments.append({
                "bill_no": bill_no,
                "date": inst_f["date"],
                "amount": str(Decimal(inst_f["amount_str"]).quantize(Decimal("0.01"))),
                "salesman": salesman,
                "created_by": current_user.name,
                "created_at": now_str,
            })
        session_bill_nos.add(bill_no)
        # loop continues — user exits via 'b' or Enter at Bill No prompt

    if not all_vouchers:
        print("\nNo vouchers to stage.")
        prompt_continue()
        return

    clear_screen()
    print(f"\n{len(all_vouchers)} voucher(s) ready to stage:")
    display_addv_summary(all_vouchers, all_installments)

    ans = read_input("\nSave to staging? (y/n): ").strip().lower()
    if ans not in ("y", "yes"):
        print("Discarded.")
        prompt_continue()
        return

    ensure_staging_dir()
    if resume_path:
        json_path = resume_path
        original_created_at = resume_data.get("created_at", now_str)
        original_created_by = resume_data.get("created_by", current_user.name)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_user = sanitize_filename_component(current_user.name)
        json_path = STAGING_DIR / f"addv{timestamp}-{safe_user}.json"
        original_created_at = now_str
        original_created_by = current_user.name

    report_data = {
        "type": "add_vouchers",
        "mode": "inline",
        "created_by": original_created_by,
        "created_at": original_created_at,
        "stage": "added",
        "stages": {"add": "done", "confirm": "", "post": ""},
        "vouchers": all_vouchers,
        "installments": all_installments,
    }
    try:
        save_report_json(json_path, report_data)
        print(f"\nStaged: {json_path.name}  ({len(all_vouchers)} voucher(s))")
    except Exception as e:
        print(f"Failed to save staging file: {e}")
    prompt_continue()


def run_import_vouchers(current_user):
    """Batch CSV import — reads vouchers CSV and optional installments CSV."""
    clear_screen()
    print("\n" + "-" * 50)
    print("Import Vouchers - Batch CSV")
    print("-" * 50)
    print("\nRequired columns — vouchers: bill_no, date, amount, beat, salesman")
    print("Required columns — installments: bill_no, date, amount, salesman")

    voucher_path = prompt_csv_file_path("vouchers")
    if voucher_path is None:
        return
    if not voucher_path:
        print("No file specified.")
        prompt_continue()
        return

    try:
        v_fields, voucher_rows = read_csv_file(voucher_path)
    except Exception as e:
        print(f"Error reading vouchers CSV: {e}")
        prompt_continue()
        return

    required_v = {"bill_no", "date", "amount", "beat", "salesman"}
    missing_v = required_v - set(v_fields)
    if missing_v:
        print(f"Missing required columns: {', '.join(sorted(missing_v))}")
        prompt_continue()
        return

    if not voucher_rows:
        print("Vouchers CSV has no data rows.")
        prompt_continue()
        return

    print(f"  {len(voucher_rows)} voucher row(s) read.")

    inst_path = prompt_csv_file_path("installments (optional)")
    if inst_path is None:
        return
    inst_rows = []
    if inst_path:
        try:
            i_fields, inst_rows = read_csv_file(inst_path)
        except Exception as e:
            print(f"Error reading installments CSV: {e}")
            prompt_continue()
            return
        required_i = {"bill_no", "date", "amount", "salesman"}
        missing_i = required_i - set(i_fields)
        if missing_i:
            print(f"Missing required installments columns: {', '.join(sorted(missing_i))}")
            prompt_continue()
            return
        print(f"  {len(inst_rows)} installment row(s) read.")

    try:
        beats = load_beats(current_user)
        salesmen = load_salesmen()
    except Exception as e:
        print(f"Error loading reference data: {e}")
        prompt_continue()
        return

    existing_bill_nos = load_all_existing_bill_nos() | load_addv_staged_bill_nos()
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    print("\nValidating...")
    errors, vouchers, installments = validate_addv_batch(
        voucher_rows, inst_rows, existing_bill_nos,
        set(beats), set(salesmen), current_user.name, now_str,
    )

    if errors:
        print(f"\n{len(errors)} error(s) found — import blocked:\n")
        for e in errors:
            print(f"  {e}")
        print("\nFix the CSV file(s) and retry.")
        prompt_continue()
        return

    clear_screen()
    print(f"\n{len(vouchers)} voucher(s) validated OK:")
    display_addv_summary(vouchers, installments)

    ans = read_input("\nSave to staging? (y/n): ").strip().lower()
    if ans not in ("y", "yes"):
        print("Import cancelled.")
        prompt_continue()
        return

    ensure_staging_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_user = sanitize_filename_component(current_user.name)
    json_path = STAGING_DIR / f"addv{timestamp}-{safe_user}.json"

    report_data = {
        "type": "add_vouchers",
        "mode": "batch",
        "created_by": current_user.name,
        "created_at": now_str,
        "stage": "added",
        "stages": {"add": "done", "confirm": "", "post": ""},
        "vouchers": vouchers,
        "installments": installments,
    }
    try:
        save_report_json(json_path, report_data)
        print(f"\nStaged: {json_path.name}  ({len(vouchers)} voucher(s))")
    except Exception as e:
        print(f"Failed to save staging file: {e}")
    prompt_continue()


def run_approve_new_vouchers(current_user):
    """Supervisor/distributor reviews and confirms a staged add-vouchers batch."""
    clear_screen()
    print("\n" + "-" * 50)
    print("Approve New Vouchers")
    print("-" * 50)

    pending = load_addv_pending_confirm()
    if not pending:
        print("\nNo new voucher batches awaiting approval.")
        prompt_continue()
        return

    report_map = {p: d for p, d in pending}
    report_paths = [p for p, _ in pending]
    labels = [
        f"{d.get('mode','?')}  |  by {d.get('created_by','')}  |  {d.get('created_at','')[:10]}  |  {len(d.get('vouchers',[]))} voucher(s)"
        for _, d in pending
    ]

    while True:
        idx = display_confirm_stage_reports(report_paths, labels, "approve")
        if idx is None:
            return

        report_path = report_paths[idx]
        report_data = report_map[report_path]

        clear_screen()
        display_addv_report(report_data)

        confirm = read_input("\nApprove these new vouchers? (y/n/b): ").strip().lower()
        if confirm == "b":
            continue
        if confirm not in ("y", "yes"):
            print("Approval skipped.")
            prompt_continue()
            return

        report_data["stages"]["confirm"] = "confirmed"
        report_data["stage"] = "confirmed"
        try:
            save_report_json(report_path, report_data)
            print(f"Approved: {report_path.name}")
        except Exception as e:
            print(f"Failed to save: {e}")
        prompt_continue()
        return


def run_post_new_vouchers(current_user):
    """Distributor writes confirmed new vouchers to data/vouchers.csv and data/installments.csv."""
    clear_screen()
    print("\n" + "-" * 50)
    print("Post New Vouchers")
    print("-" * 50)

    pending = load_addv_pending_finalize()
    if not pending:
        print("\nNo approved voucher batches awaiting posting.")
        prompt_continue()
        return

    report_map = {p: d for p, d in pending}
    report_paths = [p for p, _ in pending]
    labels = [
        f"{d.get('mode','?')}  |  by {d.get('created_by','')}  |  {d.get('created_at','')[:10]}  |  {len(d.get('vouchers',[]))} voucher(s)"
        for _, d in pending
    ]

    selected = prompt_report_selection(report_paths, labels)
    if selected is None:
        return

    report_path = selected[0]
    report_data = report_map[report_path]

    clear_screen()
    display_addv_report(report_data)

    confirm = read_input("\nPost and write to data files? (y/n/b): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Posting cancelled.")
        prompt_continue()
        return

    vouchers = report_data.get("vouchers", [])
    installments = report_data.get("installments", [])

    try:
        write_new_vouchers(vouchers)
        write_new_installments(installments)
    except Exception as e:
        print(f"Failed to write data files: {e}")
        prompt_continue()
        return

    report_data["stages"]["post"] = "confirmed"
    report_data["stage"] = "finalized"
    try:
        save_report_json(report_path, report_data)
    except Exception as e:
        print(f"Warning: failed to update staging JSON: {e}")

    try:
        archive_files([report_path])
    except Exception as e:
        print(f"Warning: could not archive {report_path.name}: {e}")

    print(f"\nPosted: {len(vouchers)} voucher(s) written to data files.")
    if installments:
        print(f"           {len(installments)} installment(s) written.")
    prompt_continue()


def run_manage_users(current_user):
    clear_screen()
    print("\n" + "-" * 50)
    print("Manage Users")
    print("-" * 50)
    print("\nThis feature is coming soon.")
    prompt_continue()


def run_manage_beats(current_user):
    clear_screen()
    print("\n" + "-" * 50)
    print("Manage Beats")
    print("-" * 50)
    print("\nThis feature is coming soon.")
    prompt_continue()
