## Schema: CLI Collection Management Tool (Design & Schemas)

TL;DR: Define a proof-of-concept data schema, validation rules, and operational design for a CSV-backed CLI collection-management tool with a staging area and Distributor-only merge. This document specifies CSV schemas, iterative delivery, and verification steps. Iteration 1 focuses on schema confirmation and test-data generation.

**Steps**
1. Finalize CSV schemas and field types (this doc).
2. Define validation rules and referential integrity checks.

**Data Schema (CSV files)**
- Storage layout:
  - `data/` — canonical CSV files: `users.csv`, `beats.csv`, `vouchers.csv`, `installments.csv`.
  - `staging/` — mirrored CSVs where Salesmen write: `vouchers.csv`, `installments.csv`.

- CSV conventions (applies to all files):
  - Delimiter: comma `,`.
  - Header row required; UTF-8 encoded.
  - Dates: ISO 8601 `YYYY-MM-DD` for dates; datetimes use `YYYY-MM-DDTHH:MM:SS`.
  - Decimal amounts: string decimal with `.`; validation uses Decimal type (no float).

- `users.csv` (data)
  - headers: `name,role`
  - types/constraints:
    - `name`: unique, ASCII-safe, no commas. Username for login.
    - `role`: `distributor` or `salesman` or `system` (enforced).
  - Initial data:
    ```
    name,role
    test,system
    distributor,distributor
    supervisor,supervisor
    saleman1,salesman
    saleman2,salesman
    saleman3,salesman
    saleman4,salesman
    saleman5,salesman
    ```
  - Note: Password management kept simple (basic hash); session-based auth via `.session` file.

- `beats.csv` (data)
  - headers: `name`
  - `name`: unique beat name.

- `vouchers.csv` (staging & data)
  - headers: `bill_no,date,amount,balance,beat,salesman,created_by,created_at`
  - notes:
    - `bill_no`: primary key for the voucher.
    - `beat`: beat name referencing `beats.csv` by name.
    - `salesman`: Salesman name responsible for the voucher.
    - `balance`: stored derived value based on installment payments.
    - No separate voucher UUID for iteration 1.

- `installments.csv` (staging & data)
  - headers: `bill_no,date,amount,salesman,created_by,created_at`
  - notes:
    - `bill_no` refers to the voucher primary key.
    - There is no separate installment ID for proof-of-concept.
    - `date` appears once per entry.
    - `salesman`: Salesman name recording the payment.
    - `created_by`: user who recorded the installment.

**Validation Rules**
- Field-level: required fields present, types correct, lengths within sane limits.
- Referential: `beat` must exist in `beats.csv`; installment `bill_no` must refer to an existing voucher `bill_no` in `data` or staging during merge.
- Business logic: `installment.amount` must not exceed the voucher balance at merge time. `balance` is derived and stored on the voucher for easier lookup.
- Uniqueness: `bill_no` must be unique across `data` and staged vouchers.
- Date windows: optionally enforce `date` values are valid and not in the future if desired.

**Future work**
- Staging, merge, CLI workflows, auth/session, audit, storage concurrency, and detailed testing are deferred to later iterations.
- This document focuses on schema confirmation and test-data generation only.

**Iteration 1 Scope**
- Focus: schema confirmation and test-data generation only.
- No CLI implementation in this iteration.
- Test-data generation CLI is separate from the core collection workflow and will be defined in later iterations.

**Update: Beats configuration**
- Beats are Distributor-only and consist of a single field: `name`.
- Canonical file: `data/beats.csv` with header: `name`

Example `data/beats.csv` content (10 beats):
name
beat1
beat2
beat3
beat4
beat5
beat6
beat7
beat8
beat9
beat10

Note: This is the sample content to be created by `scripts/init_data.py` or by a Distributor using `collmgm beat add`. The canonical schema deliberately omits `id` to keep beats referenced by name; if you prefer referential integrity via UUIDs, we can revert to an `id,name` schema instead.
