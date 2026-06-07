## Schema: CLI Collection Management Tool (Design & Schemas)

TL;DR: Define a proof-of-concept data schema, validation rules, and operational design for a CSV-backed CLI collection-management tool with a staging area and Distributor/supervisor-only merge. This document specifies CSV schemas.

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
    - `role`: `distributor` or `salesman` or `system` or `supervisor` (enforced).
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
 - Initial data:
    ```
    name
    beat1
    beat2
    .
    .
    .
    beat10
    ```

- `vouchers.csv` (staging & data)
  - headers: `bill_no,date,amount,balance,beat,salesman,created_by,created_at`
  - notes:
    - `bill_no`: primary key for the voucher.
    - `date`: date of sales, format yyyymmdd
    - `amount`: total sales, 2 places decimal
    - `beat`: beat name referencing `beats.csv` by name.
    - `salesman`: Salesman name referencing `users.csv` bye name.
    - `balance`: stored derived value based on installment payments.
    - `created_by`: login creating the record
    = `created_at`: time of record creation

- `installments.csv` (staging & data)
  - headers: `bill_no,date,amount,salesman,created_by,created_at`
  - notes:
    - `bill_no` refers to the voucher primary key.
    - `date` collection date, format yyyymmdd
    - `salesman`: Salesman name recording the payment.
    - `created_by`: login creating the record
    = `created_at`: time of record creation

**Validation Rules**
- Field-level: required fields present, types correct, lengths within sane limits.
- Referential: `beat` must exist in `beats.csv`; installment `bill_no` must refer to an existing voucher `bill_no` in `data` or staging during merge.
- Business logic: `installment.amount` must not exceed the voucher balance at merge time. `balance` is derived and stored on the voucher for easier lookup.
- Date windows: optionally enforce `date` values are valid and not in the future if desired.

**Iteration 1 Scope**
- Focus: schema confirmation and test-data generation only.
- Test-data generation CLI is separate from the core collection workflow and will be defined in later iterations.