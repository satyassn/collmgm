#!/usr/bin/env python3
"""
Test data generator for collmgm POC.

Generates vouchers and installments.
- Vouchers: For a given salesman generate 10-15 vouchers per beat per visit.
    Each beat repeats after BEATINTERVAL. Use --visits to control how many
    times each beat is visited (default 1, giving 10-15 vouchers per salesman/beat).
    Each voucher has a random amount between 5000 and 25000.
- Installments: 3-4 installments per voucher, paid on BEATINTERVAL
    - Installment amounts are calculated as 15-25% of the voucher amount

Usage:
  python scripts/generate_test_data.py [--start YYYY-MM-DD] [--months N] [--visits N] [--seed S] [--preview] [-h]
  All arguments are optional and have default values.
  --start: Start date for the data generation range. Default: 2020-01-01
  --months: Number of months to generate data for. Default: 6
  --visits: Max beat visits per salesman per beat. Default: 1
  --seed: Random seed for reproducible results. Default: 42
  --preview: Print sample rows without writing to files.
  --help: Show this help message.

Examples:
  python scripts/generate_test_data.py
  python scripts/generate_test_data.py --start 2025-12-01 --months 6 --seed 42
  python scripts/generate_test_data.py --visits 3  # 3 beat visits = 30-45 vouchers per salesman/beat
  python scripts/generate_test_data.py --preview  # Print sample rows without writing
"""

import csv
import sys
from datetime import datetime, timedelta
import random
from decimal import Decimal

from coll_store import DATA_DIR
from coll_data import load_beats, load_salesmen

BEATINTERVAL = 14  # days

USAGE = """\
Usage:
  python scripts/generate_test_data.py [--start YYYY-MM-DD] [--months N] [--visits N] [--seed S] [--preview] [-h]

Options:
  --start YYYY-MM-DD  Start date for data generation. Default: 2020-01-01
  --months N          Number of months to generate. Default: 6
  --visits N          Max beat visits per salesman per beat. Default: 1
  --seed S            Random seed for reproducible results. Default: 42
  --preview           Print sample rows without writing to files.
  -h, --help          Show this help message.

Examples:
  python scripts/generate_test_data.py
  python scripts/generate_test_data.py --start 2025-12-01 --months 6 --seed 42
  python scripts/generate_test_data.py --visits 3
  python scripts/generate_test_data.py --preview
"""

def parse_args():
    """Parse simple CLI arguments."""
    start_date_str = '2020-01-01'
    months = 6
    max_visits = 1
    seed = 42
    preview = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ('-h', '--help'):
            print(USAGE)
            sys.exit(0)
        elif args[i] == '--start' and i + 1 < len(args):
            start_date_str = args[i + 1]
            i += 2
        elif args[i] == '--months' and i + 1 < len(args):
            months = int(args[i + 1])
            i += 2
        elif args[i] == '--visits' and i + 1 < len(args):
            max_visits = int(args[i + 1])
            i += 2
        elif args[i] == '--seed' and i + 1 < len(args):
            seed = int(args[i + 1])
            i += 2
        elif args[i] == '--preview':
            preview = True
            i += 1
        else:
            print(f'Unknown argument: {args[i]}', file=sys.stderr)
            print(USAGE, file=sys.stderr)
            sys.exit(1)

    return start_date_str, months, max_visits, seed, preview


def _read_beats_with_salesman():
    """Return dict[beat_name -> salesman] from beats.csv."""
    beats_file = DATA_DIR / "beats.csv"
    beats = {}
    with beats_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            beats[row['name']] = row.get('salesman', '')
    return beats


def generate_data(start_date, end_date, beats_map, salesmen, max_visits=1):
    """Generate vouchers and installments.

    beats_map: dict[beat_name -> salesman] from _read_beats_with_salesman()
    """
    created_by = 'test'

    vouchers = []
    installments = []
    bill_counter = 1

    # Pre-validate and cache beats per salesman
    salesman_beats_map = {}
    for salesman in salesmen:
        salesman_beats = [b for b, s in beats_map.items() if s == salesman]
        if not salesman_beats:
            print(f'Warning: Salesman "{salesman}" does not have an assigned beat in beats.csv')
            exit(1)
        salesman_beats_map[salesman] = salesman_beats

    # Each beat is visited once every BEATINTERVAL days.
    # A salesman covers one beat per day, so beats are staggered across the interval.
    # E.g. with 2 beats and interval=14: beat[0] on days 0,14,28,… beat[1] on days 7,21,35,…
    for salesman in salesmen:
        beats = salesman_beats_map[salesman]
        num_beats = len(beats)
        for beat_idx, beat in enumerate(beats):
            offset = timedelta(days=(beat_idx * BEATINTERVAL) // num_beats)
            beat_date = start_date + offset
            visits = 0
            while beat_date <= end_date and visits < max_visits:
                num_vouchers = random.randint(10, 15)
                for _ in range(num_vouchers):
                    voucher_amount = Decimal(str(random.randint(5000, 25000)))
                    bill_no = f'{beat_date.strftime("%Y%m%d")}{bill_counter:03d}'
                    bill_counter += 1

                    voucher = {
                        'bill_no': bill_no,
                        'date': beat_date.isoformat(),
                        'amount': str(voucher_amount),
                        'balance': str(voucher_amount),
                        'beat': beat,
                        'salesman': salesman,
                        'created_by': created_by,
                        'created_at': f'{beat_date.isoformat()}T09:00:00'
                    }
                    vouchers.append(voucher)

                    # Generate 3-4 installments at 15-25% of voucher amount each, on BEATINTERVAL schedule
                    num_installments = random.randint(3, 4)
                    installment_date = beat_date + timedelta(days=BEATINTERVAL)

                    for _ in range(num_installments):
                        pct = Decimal(str(random.randint(15, 25))) / Decimal('100')
                        payment = (voucher_amount * pct).quantize(Decimal('0.01'))

                        installment = {
                            'bill_no': bill_no,
                            'date': installment_date.isoformat(),
                            'amount': str(payment),
                            'salesman': salesman,
                            'created_by': created_by,
                            'created_at': f'{installment_date.isoformat()}T10:00:00'
                        }
                        installments.append(installment)
                        installment_date += timedelta(days=BEATINTERVAL)

                beat_date += timedelta(days=BEATINTERVAL)
                visits += 1

    return vouchers, installments


def main():
    start_date_str, months, max_visits, seed, preview = parse_args()

    random.seed(seed)
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = start_date + timedelta(days=int(30 * months))

    beats_map = _read_beats_with_salesman()
    salesmen = load_salesmen()

    # Generate data
    vouchers, installments = generate_data(start_date, end_date, beats_map, salesmen, max_visits)

    # Recompute voucher balances by subtracting installments payments.
    # This ensures voucher['balance'] reflects actual outstanding after installments.
    payment_map = {}
    for inst in installments:
        bill = inst.get('bill_no')
        if not bill:
            continue
        try:
            amt = Decimal(str(inst.get('amount', '0')))
        except Exception:
            amt = Decimal('0')
        payment_map[bill] = payment_map.get(bill, Decimal('0')) + amt

    for v in vouchers:
        try:
            amount = Decimal(str(v.get('amount', '0')))
        except Exception:
            amount = Decimal('0')
        paid = payment_map.get(v.get('bill_no'), Decimal('0'))
        bal = amount - paid
        if bal < 0:
            bal = Decimal('0')
        # Normalize to two decimal places for consistency
        bal = bal.quantize(Decimal('0.01'))
        # If no fractional part, render as integer-like string to match existing files
        if bal == bal.to_integral():
            v['balance'] = str(bal.to_integral())
        else:
            v['balance'] = format(bal, 'f')

    if preview:
        print('Sample vouchers (first 3):')
        for v in vouchers[:3]:
            print('  ', v)
        print('\nSample installments (first 5):')
        for i in installments[:5]:
            print('  ', i)
        print(f'\nWould generate {len(vouchers)} vouchers, {len(installments)} installments (visits={max_visits})')
        return

    # Write to CSV
    vouchers_file = DATA_DIR / 'vouchers.csv'
    with vouchers_file.open('w', newline='') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['bill_no', 'date', 'amount', 'balance', 'beat', 'salesman', 'created_by', 'created_at']
        )
        writer.writeheader()
        writer.writerows(vouchers)

    installments_file = DATA_DIR / 'installments.csv'
    with installments_file.open('w', newline='') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['bill_no', 'date', 'amount', 'salesman', 'created_by', 'created_at']
        )
        writer.writeheader()
        writer.writerows(installments)

    print(f'Generated {len(vouchers)} vouchers and {len(installments)} installments (visits={max_visits})')
    print(f'  Date range: {start_date} to {end_date}')
    print(f'  Wrote to {vouchers_file} and {installments_file}')


if __name__ == '__main__':
    main()
