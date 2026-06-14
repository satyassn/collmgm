#!/usr/bin/env python3
"""
Test data generator for collmgm POC.

Generates vouchers and installments.
- Vouchers: For a given saleman generate 40-50 vouchers per beat.
    Each beat repeats after BEATINTERVAL
    Each voucher has a random amount between 5000 and 25000. 
- Installments: 3-4 installments per voucher, paid on BEATINTERVAL
    - Installment amounts are calculated as 20-30 % of the voucher amount

Usage:
  python scripts/generate_test_data.py [--start YYYY-MM-DD] [--months N] [--seed S] [--preview] [-h]
  All arguments are required for script to run but will have default values if not specified.
  --start: Start date for the data generation range. Default: 2020-01-01
  --months: Number of months to generate data for, default 6 months
  --seed: Random seed for reproducible results. default is 42
  --preview: Print sample rows without writing to files.
  --help: Show this help message.

Examples:
  python scripts/generate_test_data.py
  python scripts/generate_test_data.py --start 2025-12-01 --months 6 --seed 42
  python scripts/generate_test_data.py --preview  # Print sample rows without writing
"""

import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path
import random
from decimal import Decimal

BEATINTERVAL = 14  # days

USAGE = """\
Usage:
  python scripts/generate_test_data.py [--start YYYY-MM-DD] [--months N] [--seed S] [--preview] [-h]

Options:
  --start YYYY-MM-DD  Start date for data generation. Default: 2020-01-01
  --months N          Number of months to generate. Default: 6
  --seed S            Random seed for reproducible results. Default: 42
  --preview           Print sample rows without writing to files.
  -h, --help          Show this help message.

Examples:
  python scripts/generate_test_data.py
  python scripts/generate_test_data.py --start 2025-12-01 --months 6 --seed 42
  python scripts/generate_test_data.py --preview
"""

def parse_args():
    """Parse simple CLI arguments."""
    start_date_str = '2020-01-01'
    months = 6
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

    return start_date_str, months, seed, preview


def read_beats(beats_file='data/beats.csv'):
    """Read beat names from beats.csv."""
    beats = {}
    with open(beats_file) as f:
        reader = csv.DictReader(f)
        for row in reader:
            beats[row['name']] = row.get('salesman', '')
    return beats

def get_salesman_beats(salesman, beats_file='data/beats.csv'):
    """Get beats for a given salesman from beats.csv."""
    beats = []
    with open(beats_file) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('salesman', '') == salesman:
                beats.append(row['name'])
    return beats


def read_salesmen(users_file='data/users.csv'):
    """Read salesman names from users.csv (role='salesman')."""
    salesmen = []
    with open(users_file) as f:
        reader = csv.DictReader(f)
        for row in reader:
            role = row.get('role', '').strip().lower()
            name = row.get('name', '').strip()
            if role == 'salesman' and name:
                salesmen.append(name)

    if not salesmen:
        raise ValueError(
            'No salesman users found in data/users.csv. ' \
            'Make sure the file includes at least one row with role=salesman.'
        )
    return salesmen


def generate_data(start_date, end_date, beats, salesmen):
    """Generate vouchers and installments."""
    created_by = 'test'

    vouchers = []
    installments = []
    bill_counter = 1

    # Pre-validate and cache beats per salesman
    salesman_beats_map = {}
    for salesman in salesmen:
        salesman_beats = get_salesman_beats(salesman)
        if not salesman_beats:
            print(f'⚠️  Warning: Salesman "{salesman}" does not have an assigned beat in beats.csv')
            exit(1)
        salesman_beats_map[salesman] = salesman_beats

    # Each beat repeats every BEATINTERVAL days across the full date range
    current_date = start_date
    while current_date <= end_date:
        for salesman in salesmen:
            num_vouchers = random.randint(40, 50)
            for beat in salesman_beats_map[salesman]:
                for _ in range(num_vouchers):
                    voucher_amount = Decimal(str(random.randint(5000, 25000)))
                    bill_no = f'{current_date.strftime("%Y%m%d")}{bill_counter:03d}'
                    bill_counter += 1

                    voucher = {
                        'bill_no': bill_no,
                        'date': current_date.isoformat(),
                        'amount': str(voucher_amount),
                        'balance': str(voucher_amount),
                        'beat': beat,
                        'salesman': salesman,
                        'created_by': created_by,
                        'created_at': f'{current_date.isoformat()}T09:00:00'
                    }
                    vouchers.append(voucher)

                    # Generate 3-4 installments at 20-30% of voucher amount each, on BEATINTERVAL schedule
                    num_installments = random.randint(3, 4)
                    installment_date = current_date + timedelta(days=BEATINTERVAL)

                    for _ in range(num_installments):
                        pct = Decimal(str(random.randint(20, 30))) / Decimal('100')
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

        current_date += timedelta(days=BEATINTERVAL)

    return vouchers, installments


def main():
    start_date_str, months, seed, preview = parse_args()

    random.seed(seed)
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = start_date + timedelta(days=int(30 * months))

    # Read beats and salesmen
    beats = read_beats()
    salesmen = read_salesmen()

    
    # Generate data
    vouchers, installments = generate_data(start_date, end_date, beats, salesmen)

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
        print(f'\nWould generate {len(vouchers)} vouchers, {len(installments)} installments')
        return

    # Write to CSV
    vouchers_file = Path('data/vouchers.csv')
    with open(vouchers_file, 'w', newline='') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['bill_no', 'date', 'amount', 'balance', 'beat', 'salesman', 'created_by', 'created_at']
        )
        writer.writeheader()
        writer.writerows(vouchers)

    installments_file = Path('data/installments.csv')
    with open(installments_file, 'w', newline='') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['bill_no', 'date', 'amount', 'salesman', 'created_by', 'created_at']
        )
        writer.writeheader()
        writer.writerows(installments)

    # Print summary
    print(f'✓ Generated {len(vouchers)} vouchers and {len(installments)} installments')
    print(f'  Date range: {start_date} to {end_date}')

    print(f'\n→ Wrote to data/vouchers.csv and data/installments.csv')


if __name__ == '__main__':
    main()
