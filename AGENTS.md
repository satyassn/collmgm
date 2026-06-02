# AGENTS.md

## Purpose
This file tells AI coding agents how to work effectively in the `collmgm` repo.

## Project overview
`collmgm` is a proof-of-concept CLI collection-management tool. It uses CSV files as the primary datastore and is being developed iteratively.

### Current iteration
- Iteration 1 is limited to schema confirmation and test-data generation only.
- No collection workflow implementation, no staging/merge CLI, no audit or backup implementation in iteration 1.
- The generator writes directly into `data/*.csv` using a dedicated test data user.

## Important files
- `PLAN.md` — main design and schema specification.
- `Readme.md` — high-level project description and functional goals.
- `ITERATION1_PLAN.md` — iteration 1 scope and generator specification (when created).

## Key schema notes
- `users.csv`: `name,role`
  - initial users include `dist,distributor`, `supervisor,supervisor`, and `saleman1..saleman5`.
- `beats.csv`: `name`
  - sample beats are `beat1` through `beat10`.
- `vouchers.csv`: `bill_no,date,amount,balance,beat,salesman,created_by,created_at`
- `installments.csv`: `bill_no,date,amount,salesman,created_at`

## Development guidance
- Use `PLAN.md` as the authoritative design source.
- For iteration 1, implement only a test data generator and sample CSV files under `data/`.
- Keep dependencies minimal; prefer standard Python and built-in CSV handling.
- Do not implement merge, staging, audit, or user/beat management until iteration 2.
- Keep the project structure simple: scripts under `scripts/`, or a small package under `collmgm/`.

## What agents should do
- Read `PLAN.md` and follow the iteration 1 scope carefully.
- Ask for clarification before changing the planned iteration scope.
- When adding features, label them clearly as future work if they are outside iteration 1.

## What agents should not do
- Do not add full CLI workflows or operational merge logic in iteration 1.
- Do not create backups or audit logging yet.
- Do not change the agreed CSV schemas without explicit user approval.
