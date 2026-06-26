#!/usr/bin/env python3
"""
Collection Menu - Main entry point for collection management.

Authenticates the user at startup, then displays a role-filtered menu:

  Salesman:
    1. Generate collection report
    2. Submit collections
    3. Reports
    4. Exit

  Supervisor:
    1. Generate collection report
    2. Confirm collection start
    3. Submit collections
    4. Confirm submitted collections
    5. Reports
    6. Exit

  Distributor:
    1. Generate collection report
    2. Confirm collection start
    3. Submit collections
    4. Confirm submitted collections
    5. Finalize collection
    6. Reports
    7. Exit
"""

from coll_ui import display_main_menu, get_menu_choice
from coll_workflow import (
    run_login,
    run_coll_start, run_coll_confirm_start,
    run_coll_submit, run_coll_confirm_submit,
    run_coll_finalize, run_reports,
)

ALL_ACTIONS = [
    ("Generate collection report",    ['salesman', 'supervisor', 'distributor'], run_coll_start),
    ("Confirm collection start",      ['supervisor', 'distributor'],             run_coll_confirm_start),
    ("Submit collections",            ['salesman', 'supervisor', 'distributor'], run_coll_submit),
    ("Confirm submitted collections", ['supervisor', 'distributor'],             run_coll_confirm_submit),
    ("Finalize collection",           ['distributor'],                           run_coll_finalize),
    ("Reports",                       ['salesman', 'supervisor', 'distributor'], run_reports),
]


def main():
    while True:
        current_user = run_login()

        menu = [(label, fn) for label, roles, fn in ALL_ACTIONS if current_user.role in roles]
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
