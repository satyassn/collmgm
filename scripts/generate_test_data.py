#!/usr/bin/env python3
"""
Test data generator for collmgm POC.

Generates vouchers and installments across a configurable date range.
- Vouchers: 2–3 per beat per 2-week cycle.
- Installments: 5–8 per voucher, paid down bi-weekly.

Usage:
  python scripts/generate_test_data.py [--start YYYY-MM-DD] [--months N] [--seed S] [--preview]

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


def parse_args():
    """Parse simple CLI arguments."""
    start_date_str = None
    months = 6
    seed = None
    preview = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--start' and i + 1 < len(args):
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
            i += 1

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

    current_date = start_date
    num_vouchers_this_cycle = random.randint(30, 40)

    for salesman in salesmen:
        salesman_beats = get_salesman_beats(salesman)
        if not salesman_beats:
            print(f'⚠️  Warning: Salesman "{salesman}" does not have an assigned beat in beats.csv') 
            exit(1)

        for salesman_beat in salesman_beats:
            for _ in range(num_vouchers_this_cycle):
                voucher_amount = Decimal(str(random.randint(5000, 25000)))
                bill_no = f'{current_date.strftime("%Y%m%d")}{bill_counter:03d}'
                bill_counter += 1

                voucher = {
                    'bill_no': bill_no,
                    'date': current_date.isoformat(),
                    'amount': str(voucher_amount),
                    'balance': str(voucher_amount),
                    'beat': salesman_beat,
                    'salesman': salesman,
                    'created_by': created_by,
                    'created_at': f'{current_date.isoformat()}T09:00:00'
                }
                vouchers.append(voucher)

                # Generate 5–8 installments for this voucher
                num_installments = random.randint(5, 8)
                remaining_balance = voucher_amount
                installment_date = current_date + timedelta(days=14)

                for inst_idx in range(num_installments):
                    # Payment: 30–100% of remaining balance for intermediate installments
                    payment_pct = Decimal(str(random.randint(30, 100) / 100.0))
                    payment = Decimal(str(round(remaining_balance * payment_pct, 2)))

                    # For the last installment, usually pay off the remainder but sometimes leave a balance
                    if inst_idx == num_installments - 1:
                        # 80% chance to pay off fully, 20% chance to leave a partial balance
                        if random.random() < 0.8:
                            payment = remaining_balance
                        else:
                            # Leave some outstanding balance (10% - 90% of remaining)
                            partial_pct = Decimal(str(random.uniform(0.1, 0.9)))
                            payment = (remaining_balance * partial_pct).quantize(Decimal('0.01'))

                    installment = {
                        'bill_no': bill_no,
                        'date': installment_date.isoformat(),
                        'amount': str(payment),
                        'salesman': salesman,
                        'created_by': created_by,
                        'created_at': f'{installment_date.isoformat()}T10:00:00'
                    }
                    installments.append(installment)

                    remaining_balance -= payment
                    installment_date += timedelta(days=14)

            # Update voucher balance to what remains after all installments
            voucher['balance'] = str(max(Decimal('0'), remaining_balance))

        current_date += timedelta(days=14)

    return vouchers, installments


def main():
    start_date_str, months, seed, preview = parse_args()

    # Set random seed for reproducibility
    if seed is not None:
        random.seed(seed)

    # Determine start date
    if start_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    else:
        # Default: 6 months before today
        today = datetime.now().date()
        start_date = today - timedelta(days=int(30 * months))

    end_date = start_date + timedelta(days=int(30 * months))

    # Read beats and salesmen
    beats = read_beats()
    salesmen = read_salesmen()

    #if preview:
    #    print('Loaded salesmen:', salesmen)

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

    # beat_counts = {}
    # for v in vouchers:
    #     beat = v['beat']
    #     beat_counts[beat] = beat_counts.get(beat, 0) + 1

    # print('\nVouchers per beat:')
    # for beat in sorted(beat_counts.keys()):
    #     print(f'  {beat}: {beat_counts[beat]}')

    print(f'\n→ Wrote to data/vouchers.csv and data/installments.csv')


if __name__ == '__main__':
    main()
