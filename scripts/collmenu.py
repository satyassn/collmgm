#!/usr/bin/env python3
"""
Collection Menu - Main entry point for collection management.
==================================================
COLLECTION MANAGEMENT MENU
==================================================
1. coll-start  - Generate list of vouchers to start collection
2. coll-submit - Submit today's collections
3. coll-finalize - Review and finalize collections
4. Reports
    4.1 - Salesman - pending collections
    4.2 - Beat - pending collections
    4.3 - Collections - Pending by age
    4.4 - Collections - Pending by amount
    4.5 - Search voucher
5. Exit
==================================================
"""

import sys

from coll_ui import display_menu, get_menu_choice
from coll_workflow import (
    run_coll_start, run_coll_submit, run_coll_finalize, run_reports,
)


def main():
    while True:
        display_menu()
        choice = get_menu_choice()

        if choice == 1:
            run_coll_start()
        elif choice == 2:
            run_coll_submit()
        elif choice == 3:
            run_coll_finalize()
        elif choice == 4:
            run_reports()
        elif choice == 5:
            print("\nExiting Collection Management Menu. Goodbye!\n")
            sys.exit(0)


if __name__ == "__main__":
    main()
