#!/usr/bin/env python3
"""
Collection Menu - Main entry point for collection management.

Role-based menu items are driven by data/permissions.csv (loaded on each login).
ACTION_REGISTRY maps action keys to display labels and workflow handlers.
"""

import sys

from coll_store import load_permissions, ensure_db, MigrationError
from coll_cli import display_main_menu, get_menu_choice, build_role_menu, InputCancelled
from coll_workflow import (
    run_login,
    run_coll_start, run_coll_approve_start,
    run_coll_submit, run_coll_approve_submit,
    run_coll_post, run_reports,
    run_add_vouchers, run_import_vouchers,
    run_approve_new_vouchers, run_post_new_vouchers,
    run_manage_users, run_manage_beats,
)

ACTION_REGISTRY = [
    ("coll_start",            "Generate Collection List",  run_coll_start),
    ("coll_approve_start",    "Approve Collection List",   run_coll_approve_start),
    ("coll_submit",           "Submit Collections",        run_coll_submit),
    ("coll_approve_submit",   "Approve Collections",       run_coll_approve_submit),
    ("coll_post",             "Post Collections",          run_coll_post),
    ("add_vouchers",          "Add Vouchers",              run_add_vouchers),
    ("import_vouchers",       "Import Vouchers",           run_import_vouchers),
    ("approve_new_vouchers",  "Approve New Vouchers",      run_approve_new_vouchers),
    ("post_new_vouchers",     "Post New Vouchers",         run_post_new_vouchers),
    ("reports",               "Reports",                   run_reports),
    ("manage_users",          "Manage Users",              run_manage_users),
    ("manage_beats",          "Manage Beats",              run_manage_beats),
]


def main():
    try:
        ensure_db()
    except MigrationError as error:
        print(f"\nDatabase migration failed:\n{error}\n")
        sys.exit(1)
    while True:
        try:
            current_user = run_login()
        except InputCancelled:
            print("\nGoodbye!\n")
            sys.exit(0)
        permissions = load_permissions()
        menu = build_role_menu(current_user, permissions, ACTION_REGISTRY)
        labels = [label for label, _ in menu]

        while True:
            display_main_menu(labels, current_user)
            try:
                choice = get_menu_choice(len(labels) + 1)
            except InputCancelled:
                print("\nGoodbye!\n")
                sys.exit(0)

            if choice == len(labels) + 1:
                break

            _, fn = menu[choice - 1]
            try:
                fn(current_user)
            except InputCancelled:
                # Ctrl+C mid-workflow: unsaved work is discarded, same as
                # the existing quit-without-save path.
                print("\n(Cancelled — returning to menu.)")


if __name__ == "__main__":
    main()
