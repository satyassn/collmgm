# Scripts

Utility scripts for the collmgm POC.

## `generate_test_data.py`

Test data generator for development and POC demonstrations.

**Purpose**  
Generates realistic vouchers and installments across a 6-month (or configurable) date range. Writes directly to the SQLite database (`data/collmgm.db`) via `coll_store` — the same backend the app reads from.

**Quick Start**

Run from the project root (not from `scripts/`):

```bash
cd /home/s3teq/repos/collmgm
python scripts/generate_test_data.py
```

This generates 6 months of data backdated from today, starting with a fresh seed.

The generator reads the `users` and `beats` tables in `data/collmgm.db` (auto-migrated from `data/users.csv`/`data/beats.csv` on first run) and only uses users whose role is exactly `salesman`.

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

- Resets and repopulates the `vouchers`, `installments`, `completed_vouchers`,
  and `completed_installments` tables in `data/collmgm.db` (full reset —
  every run replaces the previous one). `users`, `beats`, and `permissions`
  are left untouched.
- Prints summary: total counts and date range.

**Data Generation Logic**

- **Timeframe**: Generates data bi-weekly over the specified period (default 6 months).
- **Vouchers**: 2–3 vouchers per beat per 2-week cycle, with random amounts (5,000–25,000).
- **Installments**: 5–8 installment payments per voucher, each paying down 30–100% of remaining balance.
- **Salesmen**: Reads all users with role `salesman` from the `users` table and randomly assigns them to vouchers.
- **Created by**: All entries use `test` as the creator (POC test user).

**Prerequisites**

- Python 3.6+
- `data/collmgm.db` must have `beats` (with beat→salesman assignments) and
  `users` (with salesman-role rows) populated — either already migrated, or
  present as `data/beats.csv`/`data/users.csv` so the script's `ensure_db()`
  call can migrate them on first run.
- Write access to `data/` directory.

**Example Output**

```
Generated 157 vouchers and 1,247 installments (visits=1)
  Date range: 2025-12-01 to 2026-06-01
  Wrote to data/collmgm.db (vouchers, installments tables)
```
