"""
Workflow coordinator layer for collection management.

Orchestrates UI ↔ data ↔ store interactions.
No direct print/input calls (delegates to coll_ui).
No direct file I/O (delegates to coll_store).
No direct CSV reads (delegates to coll_data).
"""

import os
import sys
from datetime import datetime
from decimal import Decimal

from coll_store import (
    STAGING_DIR, ARCHIVE_DIR, PRINTS_DIR,
    ensure_staging_dir, ensure_prints_dir, save_report_json,
    write_collection_text, write_print_collection_txt, sanitize_filename_component,
    _installments_path, _save_installments, _load_installments,
    _append_installments_csv, _update_vouchers_balance, _archive_completed,
    verify_user, _load_pending_start_reports, _load_pending_submit_reports,
)
from coll_data import (
    load_beats, load_salesmen, load_beats_pending_summary, _load_vouchers_by_criterion,
    _find_any_active_beat_report, _load_confirmed_start_reports,
    _load_submit_confirmed_reports,
    query_pending_by_salesman, query_pending_by_beat,
    query_pending_by_age, query_pending_by_amount,
    NUMOF_TOP_AGED_VOUCHERS, NUMOF_TOP_AMOUNT_VOUCHERS,
    search_voucher,
)
from coll_ui import (
    prompt_login,
    select_from_list, select_beat_with_summary, prompt_continue, prompt_report_selection,
    interactive_payment_editor,
    display_confirm_stage_reports, display_report_for_review,
    display_report_salesman_pending, display_report_beat_pending,
    display_report_by_age, display_report_by_amount,
    display_voucher_detail,
)


def run_login():
    """Prompt for credentials until valid. Returns authenticated User."""
    while True:
        name, password = prompt_login()
        user = verify_user(name, password)
        if user:
            return user
        print("\nInvalid username or password. Please try again.")
        input("Press Enter to retry...")


def _report_label(report_data, fallback_name):
    """Return a human-readable label for a staging report."""
    sel_type = report_data.get("selection_type", "beat")
    sel = report_data.get("selection", [])
    if sel_type == "beat_salesman" and len(sel) >= 2:
        return f"Beat: {sel[0]} | Salesman: {sel[1]}"
    return fallback_name


def run_coll_start(current_user):
    try:
        beats = load_beats(current_user)
        summary = load_beats_pending_summary()
    except Exception as error:
        print(f"Error: {error}")
        prompt_continue()
        return

    pending_start = _load_pending_start_reports()
    awaiting_beats = {d.get("selection", [None])[0] for _, d in pending_start if d.get("selection")}

    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print("\n" + "-" * 50)
        print("coll-start - Generate collection list")
        print("-" * 50)

        beat = select_beat_with_summary(beats, summary, awaiting_beats=awaiting_beats)
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
            chosen_salesman = select_from_list(salesmen_in_beat, "salesman")
            if chosen_salesman is None:
                continue  # 'b' at salesman → back to beat selection
        else:
            chosen_salesman = salesmen_in_beat[0]

        selection_type = "beat_salesman"
        selection_values = [beat, chosen_salesman]

        # Filter in-memory to chosen salesman (no second CSV read)
        vouchers = [v for v in beat_vouchers if v["salesman"] == chosen_salesman]

        existing = _find_any_active_beat_report(selection_type, selection_values)
        if existing:
            existing_path, existing_data = existing
            stages = existing_data.get("stages", {})
            in_submit_pipeline = stages.get("submit") in ("submitted", "inprogress", "confirmed")

            print(f"\nAn active collection already exists for Beat: {beat} | Salesman: {chosen_salesman}")

            if in_submit_pipeline:
                print("This report is in the submit/finalize pipeline. Complete it before starting a new collection.")
                prompt_continue()
                return

            # Report is in start stage (awaiting supervisor confirmation) — allow view or discard
            go_back = False
            while True:
                choice = input("View existing (v) / discard and create new (n) / skip (s) / back (b): ").strip().lower()
                if choice in ("s", ""):
                    print("Skipped.")
                    prompt_continue()
                    return
                elif choice == "b":
                    go_back = True
                    break
                elif choice == "v":
                    txt_path = existing_path.with_suffix(".txt")
                    os.system("cls" if os.name == "nt" else "clear")
                    display_report_for_review(existing_data, txt_path)
                    print("\n(Report is awaiting supervisor confirmation.)")
                    input("Press Enter to continue...")
                    go_back = True
                    break
                elif choice == "n":
                    existing_path.unlink()
                    txt = existing_path.with_suffix(".txt")
                    if txt.exists():
                        txt.unlink()
                    print("Existing report discarded. Generating new list...")
                    break
                else:
                    print("Please enter 'v', 'n', 's', or 'b'.")

            if go_back:
                continue  # back to beat selection

        ensure_staging_dir()
        timestamp = datetime.now().strftime("%Y%m%d")
        safe_selection = "_".join(sanitize_filename_component(v) for v in selection_values)
        base_name = f"coll{timestamp}-{selection_type}-{safe_selection}"
        json_path = STAGING_DIR / f"{base_name}.json"
        txt_path = STAGING_DIR / f"{base_name}.txt"

        start_data = {
            "stage": "start",
            "status": "new",
            "stages": {},
            "selection_type": selection_type,
            "selection": selection_values,
            "vouchers": vouchers,
        }

        try:
            save_report_json(json_path, start_data)
            write_collection_text(txt_path, [beat], [chosen_salesman], vouchers,
                                  stage="start", status="new")
        except Exception as error:
            print(f"Failed to create report files: {error}")
            prompt_continue()
            return

        os.system("cls" if os.name == "nt" else "clear")
        print(txt_path.read_text(encoding="utf-8"))

        go_back = False
        while True:
            confirm = input("Keep this report? (y/n/b to go back): ").strip().lower()
            if confirm in ("y", "yes"):
                print("Report generated. Awaiting supervisor confirmation.")
                break
            elif confirm in ("n", "no"):
                json_path.unlink()
                if txt_path.exists():
                    txt_path.unlink()
                print("Report discarded.")
                break
            elif confirm == "b":
                json_path.unlink()
                if txt_path.exists():
                    txt_path.unlink()
                go_back = True
                break
            else:
                print("Please enter 'y', 'n', or 'b'.")

        if go_back:
            os.system("cls" if os.name == "nt" else "clear")
            continue  # back to beat selection

        break  # kept or discarded — exit outer loop

    prompt_continue()


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
        os.system("cls" if os.name == "nt" else "clear")
        print("\n" + "-" * 50)
        print("Submit Collections")
        print("-" * 50)
        print("\nNo confirmed start reports found in staging/.")
        prompt_continue()
        return

    confirmed_map = {p: d for p, d in confirmed_reports}
    report_paths = list(confirmed_map.keys())
    labels = [_report_label(confirmed_map[p], p.name) for p in report_paths]

    while True:
        os.system("cls" if os.name == "nt" else "clear")
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
            out_path = PRINTS_DIR / f"print_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            write_print_collection_txt(out_path, chosen_reports)
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
        vouchers = report_data["vouchers"]
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

        installments, bookmark_bill_no, _ = _load_installments(report_path)
        start_idx = 0
        if installments:
            for v in vouchers:
                if v["bill_no"] in installments:
                    v["payment"] = installments[v["bill_no"]]
            print(f"  Loaded {len(installments)} installment(s) from prior session.")
        if bookmark_bill_no:
            bill_nos = [v["bill_no"] for v in vouchers]
            if bookmark_bill_no in bill_nos:
                start_idx = bill_nos.index(bookmark_bill_no)
                print(f"  Resuming from bookmarked record: {bookmark_bill_no}")

        print(f"\nEditing report: {report_path.name}")
        vouchers, completed, quit_idx = interactive_payment_editor(vouchers, beats, salesmen, start_idx=start_idx)

        if not completed:
            if vouchers is not None:
                try:
                    bookmark = vouchers[quit_idx]["bill_no"] if quit_idx is not None else None
                    _save_installments(report_path, vouchers, bookmark_bill_no=bookmark, inst_status="inprogress")
                    report_data.setdefault("stages", {})["start"] = "confirmed"
                    report_data.setdefault("stages", {})["submit"] = "inprogress"
                    report_data["stage"] = "submit"
                    report_data["status"] = "inprogress"
                    report_data["vouchers"] = vouchers
                    save_report_json(report_path, report_data)
                    print(f"Progress saved as installments for {report_path.name}.")
                    if bookmark:
                        print(f"Bookmarked at: {bookmark}")
                except Exception as error:
                    print(f"Failed to save installments for {report_path.name}: {error}")
            else:
                print(f"Quit without saving for {report_path.name}.")
            continue

        while True:
            save_confirm = input("\nSave these collections? (y/n/b): ").strip().lower()
            if save_confirm in ("y", "yes", "n", "no", "b"):
                break
            print("Please enter 'y', 'n', or 'b'.")
        if save_confirm in ("n", "no", "b"):
            print(f"Collections discarded for {report_path.name}.")
            continue

        try:
            _save_installments(report_path, vouchers, inst_status="complete")
            report_data.setdefault("stages", {})["start"] = "confirmed"
            report_data.setdefault("stages", {})["submit"] = "inprogress"
            report_data["stage"] = "submit"
            report_data["status"] = "inprogress"
            report_data["vouchers"] = vouchers
            save_report_json(report_path, report_data)
        except Exception as error:
            print(f"Failed to save collections for {report_path.name}: {error}")
            continue

        submit_confirm = input("Submit this report for supervisor review? (y/n/b): ").strip().lower()
        if submit_confirm not in ["y", "yes"]:
            print(f"Collections saved. Submission deferred for {report_path.name}.")
            continue

        try:
            report_data.setdefault("stages", {})["submit"] = "submitted"
            report_data["stage"] = "submit"
            report_data["status"] = "submitted"
            save_report_json(report_path, report_data)
            txt_path = report_path.with_suffix(".txt")
            write_collection_text(txt_path, beats, salesmen, vouchers,
                                  stage="submit", status="submitted")
            _save_installments(report_path, vouchers, inst_status="complete")
            print(f"Submitted for supervisor review: {report_path.name}")
        except Exception as error:
            print(f"Failed to submit report {report_path.name}: {error}")

    prompt_continue()


def run_coll_finalize(current_user):
    os.system("cls" if os.name == "nt" else "clear")
    print("\n" + "-" * 50)
    print("coll-finalize - Finalize collections")
    print("-" * 50)

    submit_reports = _load_submit_confirmed_reports()
    if not submit_reports:
        print("\nNo submitted reports ready for finalization.")
        prompt_continue()
        return

    report_map = {p: d for p, d in submit_reports}
    report_paths = list(report_map.keys())
    labels = [_report_label(report_map[p], p.name) for p in report_paths]
    selected_paths = prompt_report_selection(report_paths, labels)
    if selected_paths is None:
        return

    for report_path in selected_paths:
        report_data = report_map[report_path]
        vouchers = report_data["vouchers"]

        txt_path = report_path.with_suffix(".txt")
        print(f"\nReport: {report_path.name}")
        if txt_path.exists():
            print(txt_path.read_text(encoding="utf-8"))
        else:
            print(f"  (Text report not found. Vouchers: {len(vouchers)})")

        confirm = input("Ready to finalize these collections? (y/n/b): ").strip().lower()
        if confirm not in ["y", "yes"]:
            print(f"Finalization skipped for {report_path.name}.")
            continue

        try:
            _append_installments_csv(vouchers)
            completed_bill_nos = _update_vouchers_balance(vouchers)
            if completed_bill_nos:
                _archive_completed(completed_bill_nos)
        except Exception as error:
            print(f"Failed to update data files for {report_path.name}: {error}")
            continue

        report_data.setdefault("stages", {})["finalize"] = "confirmed"
        report_data["stage"] = "finalize"
        report_data["status"] = "confirmed"
        try:
            save_report_json(report_path, report_data)
        except Exception as error:
            print(f"Failed to update staging JSON for {report_path.name}: {error}")
            continue

        ARCHIVE_DIR.mkdir(exist_ok=True)
        for src in [report_path, _installments_path(report_path), txt_path]:
            if src.exists():
                try:
                    src.rename(ARCHIVE_DIR / src.name)
                except Exception as error:
                    print(f"  Warning: could not archive {src.name}: {error}")

        total = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
        paid_count = sum(1 for v in vouchers if v.get("payment"))
        print(f"Finalized: {report_path.name} — {paid_count} records, total {total}")


def run_coll_confirm_start(current_user):
    os.system("cls" if os.name == "nt" else "clear")
    print("\n" + "-" * 50)
    print("coll-confirm-start - Confirm collection start reports")
    print("-" * 50)

    pending = _load_pending_start_reports()
    if not pending:
        print("\nNo reports awaiting start confirmation.")
        prompt_continue()
        return

    report_map = {p: d for p, d in pending}
    report_paths = list(report_map.keys())
    labels = [_report_label(report_map[p], p.name) for p in report_paths]

    while True:
        idx = display_confirm_stage_reports(report_paths, labels, "confirm start")
        if idx is None:
            return

        report_path = report_paths[idx]
        report_data = report_map[report_path]
        txt_path = report_path.with_suffix(".txt")

        os.system("cls" if os.name == "nt" else "clear")
        display_report_for_review(report_data, txt_path)

        confirm = input("\nConfirm this start report? (y/n/b): ").strip().lower()
        if confirm == "b":
            continue
        if confirm not in ("y", "yes"):
            print("Confirmation skipped.")
            prompt_continue()
            return

        report_data["status"] = "confirmed"
        report_data.setdefault("stages", {})["start"] = "confirmed"
        try:
            save_report_json(report_path, report_data)
            sel = report_data.get("selection", [])
            beat = sel[0] if len(sel) > 0 else ""
            salesman = sel[1] if len(sel) > 1 else ""
            write_collection_text(txt_path, [beat], [salesman], report_data["vouchers"],
                                  stage="start", status="confirmed")
            print(f"Start report confirmed: {report_path.name}")
        except Exception as error:
            print(f"Failed to confirm report: {error}")
        prompt_continue()
        return


def run_coll_confirm_submit(current_user):
    os.system("cls" if os.name == "nt" else "clear")
    print("\n" + "-" * 50)
    print("coll-confirm-submit - Confirm submitted collections")
    print("-" * 50)

    pending = _load_pending_submit_reports()
    if not pending:
        print("\nNo submitted collections awaiting confirmation.")
        prompt_continue()
        return

    report_map = {p: d for p, d in pending}
    report_paths = list(report_map.keys())
    labels = [_report_label(report_map[p], p.name) for p in report_paths]

    while True:
        idx = display_confirm_stage_reports(report_paths, labels, "confirm collections")
        if idx is None:
            return

        report_path = report_paths[idx]
        report_data = report_map[report_path]
        txt_path = report_path.with_suffix(".txt")

        os.system("cls" if os.name == "nt" else "clear")
        display_report_for_review(report_data, txt_path)

        confirm = input("\nConfirm these submitted collections? (y/n/b): ").strip().lower()
        if confirm == "b":
            continue
        if confirm not in ("y", "yes"):
            print("Confirmation skipped.")
            prompt_continue()
            return

        report_data.setdefault("stages", {})["submit"] = "confirmed"
        report_data["stage"] = "submit"
        report_data["status"] = "confirmed"
        try:
            save_report_json(report_path, report_data)
            print(f"Collections confirmed: {report_path.name}")
        except Exception as error:
            print(f"Failed to confirm collections: {error}")
        prompt_continue()
        return


def run_report_salesman_pending():
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
        print(f"\nNo pending vouchers found for salesman: {salesman}")
        prompt_continue()
        return
    display_report_salesman_pending(salesman, grouped)
    prompt_continue()


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


def run_report_collections_by_age():
    print("\n" + "-" * 50)
    print("Report: Collections - Pending by Age")
    print("-" * 50)
    top, total = query_pending_by_age(NUMOF_TOP_AGED_VOUCHERS)
    if not top:
        print("\nNo pending vouchers found.")
        prompt_continue()
        return
    display_report_by_age(top, total, NUMOF_TOP_AGED_VOUCHERS)
    prompt_continue()


def run_report_collections_by_amount():
    print("\n" + "-" * 50)
    print("Report: Collections - Pending by Amount")
    print("-" * 50)
    top, total = query_pending_by_amount(NUMOF_TOP_AMOUNT_VOUCHERS)
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
    bill_no = input("Enter bill number: ").strip()
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


def run_reports():
    """Reports sub-menu loop."""
    from coll_ui import get_reports_submenu_choice
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
        elif choice == 5:
            run_voucher_search()
