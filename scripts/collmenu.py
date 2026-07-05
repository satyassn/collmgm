#!/usr/bin/env python3
"""
Collection Menu - Main entry point for collection management.

Role-based menu items are driven by data/permissions.csv (loaded on each login).
ACTION_REGISTRY maps action keys to display labels and workflow handlers.
"""

from coll_store import load_permissions, ensure_db
from coll_cli import display_main_menu, get_menu_choice, build_role_menu
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
    ensure_db()
    while True:
        current_user = run_login()
        permissions = load_permissions()
        menu = build_role_menu(current_user, permissions, ACTION_REGISTRY)
        labels = [label for label, _ in menu]

        while True:
            display_main_menu(labels, current_user)
            choice = get_menu_choice(len(labels) + 1)

            if choice == len(labels) + 1:
                break

            _, fn = menu[choice - 1]
            fn(current_user)


if __name__ == "__main__":
    main()
