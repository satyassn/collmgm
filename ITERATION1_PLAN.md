# Iteration 1 Plan — Test-data generation

Purpose: produce a schema-accurate dataset for a 6-month PoC and a CLI helper to generate it. The dataset is written directly into `data/` (test user writes to `data/*.csv`), not to `staging/`.

Scope
- Confirmed schemas: `users.csv`, `beats.csv`, `vouchers.csv`, `installments.csv` as defined in `schema.md`.
- Add test user: `test,system` — present in `data/users.csv`. The generator uses `test` as `created_by` when writing into `data/`.
- Implement a `generate-test-data` script/CLI that produces realistic vouchers and installments according to the parameters in this plan.
- Do NOT implement staging/merge, backups, audit, or live CLI workflows in this iteration.

Test-data requirements
- Time span: generate data covering 6 months.
- Beats: use the 10 predefined beats in `data/beats.csv`.
- Salesmen: use `saleman1..saleman5` for generation.
- Volume: 15–20 vouchers per beat every 2 weeks; each voucher with 5–10 installment payments.
- Installment payments: 30–100% of current voucher balance, bi-weekly schedule.

Files to produce
- `data/users.csv` (include `test,system`).
- `data/beats.csv` (beat1..beat10).
- `data/vouchers.csv` — populated with generated vouchers; headers: `bill_no,date,amount,balance,beat,salesman,created_by,created_at`.
- `data/installments.csv` — populated; headers: `bill_no,date,amount,salesman,created_at`.

Generator CLI (spec)
- `generate-test-data --start YYYY-MM-DD --months N --out data/ --seed S`
- Outputs: writes CSVs to `data/` and prints a short summary (counts per file). It does not alter `staging/`.
- Options: `--preview` to print sample rows without writing.

Verification
- Print summary: total vouchers, total installments, per-beat counts.
- Optional `--preview` to inspect sample rows.

Notes
- The authoritative schema is in `schema.md` — keep generator in sync with it.
- Audit, UUID ids, and merge logic are deferred to later iterations.
