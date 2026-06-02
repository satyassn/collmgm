# Scripts

Utility scripts for the collmgm POC.

## `generate_test_data.py`

Test data generator for development and POC demonstrations.

**Purpose**  
Generates realistic vouchers and installments across a 6-month (or configurable) date range. Writes directly to `data/vouchers.csv` and `data/installments.csv`.

**Quick Start**

Run from the project root (not from `scripts/`):

```bash
cd /home/s3teq/repos/collmgm
python scripts/generate_test_data.py
```

This generates 6 months of data backdated from today, starting with a fresh seed.

The generator reads `data/users.csv` and only uses users whose role is exactly `salesman`.

**Options**

```bash
# Specify start date and duration
python scripts/generate_test_data.py --start 2025-12-01 --months 6

# Use a fixed seed for reproducible results
python scripts/generate_test_data.py --start 2025-12-01 --seed 42

# Preview sample rows without writing to disk
python scripts/generate_test_data.py --preview
```

**Output**

- Writes to `data/vouchers.csv` (overwrites existing file).
- Writes to `data/installments.csv` (overwrites existing file).
- Prints summary: total counts and per-beat breakdown.

**Data Generation Logic**

- **Timeframe**: Generates data bi-weekly over the specified period (default 6 months).
- **Vouchers**: 2–3 vouchers per beat per 2-week cycle, with random amounts (5,000–25,000).
- **Installments**: 5–8 installment payments per voucher, each paying down 30–100% of remaining balance.
- **Salesmen**: Reads all users with role `salesman` from `data/users.csv` and randomly assigns them to vouchers.
- **Created by**: All entries use `test` as the creator (POC test user).

**Prerequisites**

- Python 3.6+
- `data/beats.csv` must exist with beat names (header: `name`).
- `data/users.csv` must exist with users and roles (the script reads salesman names from users with role `salesman`).
- Write access to `data/` directory.

**Example Output**

```
✓ Generated 157 vouchers and 1,247 installments
  Date range: 2025-12-01 to 2026-06-01

Vouchers per beat:
  beat1: 16
  beat2: 15
  beat3: 17
  ...

→ Wrote to data/vouchers.csv and data/installments.csv
```
