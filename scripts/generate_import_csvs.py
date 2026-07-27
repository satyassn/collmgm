#!/usr/bin/env python3
"""
Onboarding CSV generator for the "Import Vouchers" CLI feature.

Generates two CSV files — a vouchers CSV and an optional installments CSV —
in the exact format expected by run_import_vouchers() (coll_workflow.py):
  vouchers CSV:      bill_no, date, amount, beat, salesman
  installments CSV:  bill_no, date, amount, salesman

This does NOT touch data/collmgm.db. The generated files are meant to be fed
into the app's own Import Vouchers -> Approve New Vouchers -> Post New Vouchers
pipeline, which is how a new customer-site installation gets its existing
vouchers (and any already-collected payments against them) into master data.

- Vouchers: 10-15 per beat (each beat has one assigned salesman in beats.csv),
    constant amount, issue date spread between --start and --end.
- Installments: 0-5 per voucher, constant amount each, dated between the
    voucher's issue date and --end (never in the future, never before the
    voucher's own date).

Usage:
  python scripts/generate_import_csvs.py [options]

Options:
  --start YYYY-MM-DD        Earliest voucher date. Default: today - 2 months.
  --end YYYY-MM-DD          Latest voucher/installment date. Default: today.
  --voucher-amount N        Constant voucher amount. Default: 10000.
  --installment-amount N    Constant per-installment amount. Default: 2000.
  --min-installments N      Min installments per voucher. Default: 0.
  --max-installments N      Max installments per voucher. Default: 5.
  --min-vouchers N          Min vouchers per beat. Default: 10.
  --max-vouchers N          Max vouchers per beat. Default: 15.
  --seed S                  Random seed for reproducible results. Default: 42.
  --out-vouchers PATH       Output path for the vouchers CSV. Default: import/vouchers.csv
  --out-installments PATH   Output path for the installments CSV. Default: import/installments.csv
  --preview                 Print sample rows/counts without writing files.
  -h, --help                Show this help message.

Examples:
  python scripts/generate_import_csvs.py
  python scripts/generate_import_csvs.py --preview
  python scripts/generate_import_csvs.py --start 2026-01-01 --end 2026-03-01
"""

import csv
import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from coll_store import load_beats_raw

VOUCHER_FIELDS = ["bill_no", "date", "amount", "beat", "salesman"]
INSTALLMENT_FIELDS = ["bill_no", "date", "amount", "salesman"]

USAGE = __doc__


def parse_args():
    today = date.today()
    opts = {
        "start": today - timedelta(days=60),
        "end": today,
        "voucher_amount": Decimal("10000"),
        "installment_amount": Decimal("2000"),
        "min_installments": 0,
        "max_installments": 5,
        "min_vouchers": 10,
        "max_vouchers": 15,
        "seed": 42,
        "out_vouchers": Path("import/vouchers.csv"),
        "out_installments": Path("import/installments.csv"),
        "preview": False,
    }

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            sys.exit(0)
        elif arg == "--start" and i + 1 < len(args):
            opts["start"] = datetime.strptime(args[i + 1], "%Y-%m-%d").date()
            i += 2
        elif arg == "--end" and i + 1 < len(args):
            opts["end"] = datetime.strptime(args[i + 1], "%Y-%m-%d").date()
            i += 2
        elif arg == "--voucher-amount" and i + 1 < len(args):
            opts["voucher_amount"] = Decimal(args[i + 1])
            i += 2
        elif arg == "--installment-amount" and i + 1 < len(args):
            opts["installment_amount"] = Decimal(args[i + 1])
            i += 2
        elif arg == "--min-installments" and i + 1 < len(args):
            opts["min_installments"] = int(args[i + 1])
            i += 2
        elif arg == "--max-installments" and i + 1 < len(args):
            opts["max_installments"] = int(args[i + 1])
            i += 2
        elif arg == "--min-vouchers" and i + 1 < len(args):
            opts["min_vouchers"] = int(args[i + 1])
            i += 2
        elif arg == "--max-vouchers" and i + 1 < len(args):
            opts["max_vouchers"] = int(args[i + 1])
            i += 2
        elif arg == "--seed" and i + 1 < len(args):
            opts["seed"] = int(args[i + 1])
            i += 2
        elif arg == "--out-vouchers" and i + 1 < len(args):
            opts["out_vouchers"] = Path(args[i + 1])
            i += 2
        elif arg == "--out-installments" and i + 1 < len(args):
            opts["out_installments"] = Path(args[i + 1])
            i += 2
        elif arg == "--preview":
            opts["preview"] = True
            i += 1
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            sys.exit(1)

    if opts["end"] > date.today():
        print("--end cannot be in the future (Import Vouchers rejects future dates).", file=sys.stderr)
        sys.exit(1)
    if opts["start"] > opts["end"]:
        print("--start must be on or before --end.", file=sys.stderr)
        sys.exit(1)

    return opts


def _random_date(start, end):
    span = (end - start).days
    if span <= 0:
        return start
    return start + timedelta(days=random.randint(0, span))


def generate_data(beats_map, opts):
    """beats_map: dict[beat_name -> salesman], from load_beats_raw()."""
    vouchers = []
    installments = []
    bill_counter = 1

    for beat, salesman in beats_map.items():
        if not salesman:
            print(f'Warning: beat "{beat}" has no assigned salesman in beats.csv — skipping.')
            continue

        num_vouchers = random.randint(opts["min_vouchers"], opts["max_vouchers"])
        for _ in range(num_vouchers):
            voucher_date = _random_date(opts["start"], opts["end"])
            bill_no = f'{voucher_date.strftime("%Y%m%d")}{bill_counter:04d}'
            bill_counter += 1

            vouchers.append({
                "bill_no": bill_no,
                "date": voucher_date.isoformat(),
                "amount": str(opts["voucher_amount"]),
                "beat": beat,
                "salesman": salesman,
            })

            num_installments = random.randint(opts["min_installments"], opts["max_installments"])
            if num_installments:
                inst_dates = sorted(_random_date(voucher_date, opts["end"]) for _ in range(num_installments))
                for inst_date in inst_dates:
                    installments.append({
                        "bill_no": bill_no,
                        "date": inst_date.isoformat(),
                        "amount": str(opts["installment_amount"]),
                        "salesman": salesman,
                    })

    return vouchers, installments


def _write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    opts = parse_args()
    random.seed(opts["seed"])

    beats_map = {row["name"]: row.get("salesman", "") for row in load_beats_raw()}
    if not beats_map:
        print("No beats found in the database.", file=sys.stderr)
        sys.exit(1)

    vouchers, installments = generate_data(beats_map, opts)

    if opts["preview"]:
        print("Sample vouchers (first 3):")
        for v in vouchers[:3]:
            print("  ", v)
        print("\nSample installments (first 5):")
        for i in installments[:5]:
            print("  ", i)
        print(f'\nWould write {len(vouchers)} vouchers to {opts["out_vouchers"]}')
        print(f'Would write {len(installments)} installments to {opts["out_installments"]}')
        return

    _write_csv(opts["out_vouchers"], VOUCHER_FIELDS, vouchers)
    _write_csv(opts["out_installments"], INSTALLMENT_FIELDS, installments)

    print(f'Wrote {len(vouchers)} vouchers to {opts["out_vouchers"]}')
    print(f'Wrote {len(installments)} installments to {opts["out_installments"]}')
    print(f'  Date range: {opts["start"]} to {opts["end"]}')
    print("  Next: CLI menu -> Import Vouchers -> Approve New Vouchers -> Post New Vouchers")


if __name__ == "__main__":
    main()
