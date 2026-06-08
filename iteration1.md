# Iteration 1 Plan — Test-data generation

Purpose: produce a schema-accurate dataset for a 6-month PoC and a CLI helper to generate it. The dataset is written directly into `data/` (test user writes to `data/*.csv`), not to `staging/`.

Scope
- Confirmed schemas: as defined in `schema.md`.
- Implement a `generate-test-data` script/CLI that produces realistic vouchers and installments according to the parameters in this plan.
- Generated data should adhere to validation rules in `schema.md`

Test-data requirements
- Time span: generate data covering 6 months.
- Beats: use initial beats in schema.md.
- users : use initial data in schmea.md
- Volume: 15–20 vouchers per beat every 2 weeks; each voucher with 5–10 installment payments.
- Installment payments: 30–100% of current voucher balance, bi-weekly schedule.

Files to produce
- `data/users.csv` with initial beats in schema.md.
- `data/beats.csv` witn initial beats in schema.md.
- `data/vouchers.csv` — populated with this tool 
- `data/installments.csv` — populated with this tool

Generator CLI (spec)
- `generate-test-data --start YYYY-MM-DD --months N --out data/ --seed S`
- Outputs: writes CSVs to `data/` and prints a short summary (counts per file). It does not alter `staging/`.
- Options: `--preview` to print sample rows without writing.

Verification
- Print summary: total vouchers, total installments, per-beat counts.
- Optional `--preview` to inspect sample rows.

