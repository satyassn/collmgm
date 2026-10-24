## Schema: CLI Collection Management Tool (Design & Schemas)

TL;DR: Define a proof-of-concept data schema, validation rules, and operational design for a CSV-backed CLI collection-management tool with a staging area and Distributor/supervisor-only merge. This document specifies CSV schemas.

**Steps**
1. Finalize CSV schemas and field types (this doc).
2. Define validation rules and referential integrity checks.

**Data Schema (CSV files)**
- Storage layout:
  - `data/` — canonical CSV files: `users.csv`, `beats.csv`, `vouchers.csv`, `installments.csv`, `checks.csv`.
  - `staging/` — mirrored CSVs where Salesmen write: `vouchers.csv`, `installments.csv`.

> **SQLite note:** Master data (`users`, `beats`, `vouchers`, `installments`,
> `completed_vouchers`, `completed_installments`, `checks`) now lives in
> `data/collmgm.db`, migrated from these CSVs on first run (see
> `coll_store.py`). The CSV schemas below remain the source of truth for
> field names/types. Two early exceptions to the "schema enhancements
> deferred" roadmap milestone, made explicitly at user request: the SQLite
> `beats` table also carries a `salesman` column (the beat→salesman mapping
> that `beats.csv` has always had), and a `permissions` table (mirroring
> `permissions.csv`'s `role,action_key` columns) was added since permissions
> are also now DB-backed like the other master data. `checks` (iteration5)
> is a later, explicitly-approved master-data addition — see `checks.csv`
> below — unlike `corrections`/`amendments`/`amendment_requests`, which are
> SQLite-only audit tables with no CSV counterpart.

- `corrections` (SQLite only, no CSV counterpart — added iteration3 with explicit approval)
  - columns: `id` (INTEGER PK AUTOINCREMENT), `kind`
    (`installment_amount | installment_delete | installment_add | voucher_amount | collection_amount`),
    `bill_no`, `report_stem`, `origin_stage` (audit-only context captured at
    raise time), `installment_id` (references the target installment's
    surrogate id for the edit/delete kinds), `old_json` / `new_json`
    (raise-time snapshot and requested change), `note`, `status`
    (`open | applied | rejected | withdrawn`), `requested_by`, `requested_at`,
    `resolved_by`, `resolved_at`, `resolution_note`.
  - Purpose: supervisor-raised correction requests. The four master-data
    kinds (raiseable only from Approve Collection List) are applied or
    rejected by the distributor: applying re-checks `old_json` against the
    current row (stale requests refuse), performs the change, and recomputes
    the voucher balance (`amount − SUM(installments)`, negative refused) in
    one transaction with the status flip. `collection_amount` (raiseable only
    from Approve Collections) instead edits the STAGED report's payment for
    this cycle — report JSON + TXT + installments sidecar, then the status
    flip; staging files and the DB cannot share a transaction, so a crash
    between surfaces as an explicit snapshot conflict on retry. Resolved rows
    are never deleted — they are the audit trail. An open request blocks web
    approval of any staging report containing its `bill_no`.
  - Widening the `kind` CHECK for installed DBs is handled by
    `_migrate_corrections_kinds` (self-detecting table rebuild via
    `sqlite_master`; no `user_version` bump).
  - Permission keys: `raise_correction` (supervisor, distributor; also the
    view permission for the corrections screens); `apply_correction`
    (distributor only) resolves the master-data kinds; `collection_amount`
    is resolved by anyone holding `coll_approve_submit` (supervisor,
    distributor) — or by Return-to-salesman, which auto-settles the request
    when the resubmitted payment matches the requested value.

- `amendments` (SQLite only, no CSV counterpart — added iteration4 with
  explicit approval)
  - columns: `id` (INTEGER PK AUTOINCREMENT), `bill_no`, `old_json` / `new_json`
    (full before/after `{"voucher": {...}, "installments": [...]}` state, not
    a per-field diff), `note`, `amended_by`, `amended_at`.
  - Purpose: the distributor's raw, single-voucher editor ("Amend Voucher") —
    all voucher fields (`date`/`amount`/`beat`/`salesman`; `bill_no` is the
    immutable PK) plus full control of that voucher's installments (edit,
    delete, add), submitted as one atomic transaction. No `status` column:
    unlike `corrections` there is no separate raise→review lifecycle — a row
    exists iff the edit committed, written inside the same transaction as the
    master change, and it IS the audit trail (rows are never deleted).
    Committing re-checks a load-time snapshot (`AmendmentConflict` on drift,
    mirroring `CorrectionConflict`), validates every field, mutates, and
    recomputes the balance (`amount − SUM(installments)`, negative refused —
    same `_recompute_voucher_balance` corrections use).
  - Gate: `GET /coll/amend/{bill_no}` refuses to render the editor while any
    open **master-data** correction (`installment_amount` / `installment_delete`
    / `installment_add` / `voucher_amount`) exists on that bill — it redirects
    straight to that correction's review page instead, since an amendment
    could make its snapshot stale. An open `collection_amount` request does
    not gate (it concerns only the current cycle's staged payment, which an
    amendment never touches).
  - Permission key: `amend_voucher` (distributor only) gates the menu card,
    every `/coll/amend*` route, and the amendment history screens.

- `amendment_requests` (SQLite only, no CSV counterpart — added iteration4b,
  a raise→resolve lifecycle in front of the distributor-only `amend_voucher`
  editor)
  - columns: `id` (INTEGER PK AUTOINCREMENT), `bill_no`, `note`, `status`
    (`open | applied | rejected | withdrawn`), `requested_by`, `requested_at`,
    `resolved_by`, `resolved_at`, `resolution_note`, `linked_amendment_id`
    (the `amendments` row that resolved the request, when auto-settled).
  - Purpose: unlike `corrections` there is no structured `kind`/`old_json`/
    `new_json` — a supervisor or salesman flags a bill_no with a free-text
    note describing what looks wrong; the distributor reads it and makes
    whatever edit is warranted through the existing raw Amend Voucher editor.
    A request is **not** applied through this table directly: every open
    request on a bill is auto-marked `applied` (linked to the new amendment
    row) as a side effect of `amend_voucher()` landing any amendment on that
    bill_no — the raiser's note need not match the specific fields changed.
    The distributor may instead **reject** a request outright (stamping
    `resolution_note`) without amending; the raiser may **withdraw** their
    own open request. Resolved rows are never deleted — they are the audit
    trail. Not tied to any staging report or approval screen (the raw
    editor itself isn't stage-gated, so raising a request against it isn't
    either) — reachable any time via its own menu entry, from any voucher
    lookup, and as a cross-link from the Correction Requests raise form
    (Approve Collection List context only, since Approve Collections'
    `collection_amount` kind is unrelated to a full voucher/installment
    edit).
  - Gate: none. Open amendment requests do not block verification
    checkboxes or report approval (unlike corrections) and do not block
    opening the raw editor — they only surface as an informational banner
    on `GET /coll/amend/{bill_no}` linking to the request(s).
  - Permission keys: `raise_amendment_request` (supervisor, salesman) gates
    raising/withdrawing and doubles as a view permission for the Amendment
    Requests list/review screens; `amend_voucher` (distributor only, already
    existing) doubles as the resolve permission (reject, and implicitly the
    "go amend" action via the existing editor) and also as a view
    permission, so the distributor can see requests without holding
    `raise_amendment_request`.

- `settings` (SQLite only, no CSV counterpart — added with Collection Health;
  same "deliberately not in the v1 constraint-rebuild table set" rationale as
  `corrections`/`amendments`/`amendment_requests`, since it's a purely
  additive table)
  - columns: `credit_period_days` (INTEGER, default `30`), `visit_method`
    (`accurate | approximate`, default `accurate`), `zero_visit_bucket`
    (`red | yellow`, default `yellow`) — the physical column name is
    unchanged from when it only governed the zero-visit edge case, but
    `coll_store.get_collection_health_settings()` aliases it to
    `last_visit_color` in every dict it returns, since every Python-facing
    name (`coll_data.LAST_VISIT_COLORS`, `classify_voucher_health`'s
    `last_visit_color` parameter, the `/manage/credit-period` form field) now
    uses that clearer name. Don't be surprised by the mismatch if inspecting
    the DB directly.
  - Purpose: single global row (no PK; `coll_store._backfill_settings_row`
    guarantees exactly one row exists) backing the Collection Health report.
    `credit_period_days` is how many days a voucher is allowed before it's
    overdue. `visit_method` picks how remaining beat visits before that
    deadline are counted: `accurate` walks each beat's real cadence schedule
    (`coll_data.next_due_date`), `approximate` divides the days left by a
    flat 7/14/30-day interval per cadence type. `last_visit_color` decides how
    to color the ONE visit that is the last realistic chance to pay within
    the credit period, and shifts the whole green/yellow boundary to match:
    `yellow` (default) keeps today's behavior — 2+ visits remaining is green,
    1 (or 0) is yellow, only turning red once the credit period has actually
    expired; `red` shifts the boundary out by one visit — 3+ is green,
    exactly 2 is yellow, 1 (or 0) turns red immediately instead of waiting
    for the deadline to pass.
  - Lifecycle management (web-only, distributor-only, via
    `manage_credit_period` permission): view/update all three fields
    together at `/manage/credit-period`.

- CSV conventions (applies to all files):
  - Delimiter: comma `,`.
  - Header row required; UTF-8 encoded.
  - Dates: ISO 8601 `YYYY-MM-DD` for dates; datetimes use `YYYY-MM-DDTHH:MM:SS`.
  - Decimal amounts: string decimal with `.`; validation uses Decimal type (no float).

- `users.csv` (data)
  - headers: `name,role,password_hash,must_change_password`
  - types/constraints:
    - `name`: unique, ASCII-safe, no commas. Username for login.
    - `role`: `distributor` or `salesman` or `system` or `supervisor` (enforced).
    - `password_hash`: `salt_hex:hash_hex`, PBKDF2-HMAC-SHA256 (`coll_store.hash_password`/`_verify_password`). Only `distributor`/`supervisor`/`salesman` roles can authenticate (`verify_user`) — `system` never can.
    - `must_change_password`: `0`/`1` (SQLite `INTEGER`, additive column — see `coll_store._backfill_must_change_password`). Set to `1` whenever a user is created or has their password reset by a distributor (web `/manage/users`); cleared to `0` when the user successfully changes their own password (web `/profile`). Enforced web-only: `coll_api._require()` redirects any user with this flag set to `/profile` before allowing any other route. The CLI does not read or enforce this column.
    - `secret_question` / `secret_answer_hash` (SQLite `TEXT`, nullable, additive columns — see `coll_store._backfill_secret_question_columns`; DB-only, not in `users.csv`): the distributor's "Forgot password?" recovery. Only ever set on the `distributor` row — at web `/register` (required for new installs) or from `/profile` (existing installs; needs the current password). `secret_question` is free text (5-200 chars) shown on the `/forgot-password` page; `secret_answer_hash` is the PBKDF2 `salt:hash` (same scheme as `password_hash`) of the answer trimmed, case-folded and whitespace-collapsed (3-100 chars), never the plain answer. NULL means no reset is available and the login screen hides the link. Five wrong answers lock the reset flow for 15 minutes (in server memory, not stored). Web-only; the CLI has no forgot-password flow.
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
  - Note: sessions are in-memory server-side tokens held in a browser cookie (`coll_api._sessions`, process-local, reset on server restart) — not a `.session` file.
  - Lifecycle management (web-only, distributor-only, via `manage_users` permission): create/edit-role/delete/reset-password at `/manage/users*`. `name` is the immutable primary key — "edit" changes role only. Delete is a hard delete, blocked when the user is referenced in `beats.salesman`, any `vouchers`/`installments`/`completed_vouchers`/`completed_installments` `salesman`/`created_by` column, is the caller's own account, or has role `distributor`. Audit-trail-only references (`corrections.requested_by`, `amendments.amended_by`, `amendment_requests.requested_by`/`resolved_by`) do not block deletion — those are frozen text snapshots, not live joins.
  - Single permanent distributor: exactly one `distributor` account may ever exist, created only by the one-time `/register` bootstrap (`coll_store.register_first_distributor`, only reachable while the `users` table is empty). `create_user`/`update_user_role` (`ASSIGNABLE_ROLES = ('supervisor', 'salesman')`) reject `role='distributor'` outright, so the role can never be assigned or reassigned after bootstrap, and `delete_user` unconditionally refuses to delete a `distributor` account — not merely while it's the last one.

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
  - Collection cadence (SQLite columns added with the cadence migration; not in the CSV headers above): `cadence` = `weekly` (default) | `biweekly` | `monthly` (`biweekly` was named `fortnightly` before an additive migration renamed existing rows — same weekday-every-other-week behavior, clearer word); `cadence_day` = `mon`..`sun`, required by every cadence (weekly/biweekly recur on this weekday; monthly recurs on this weekday in a specific week — blank otherwise); `cadence_week` = `1`..`4`, which week of the month a monthly beat's collection day falls on (blank otherwise; monthly-only); `cadence_start` = `YYYY-MM-DD`, an *optional* explicit first due date for any cadence, used only while there's no collection history yet — left blank, the next due date is auto-computed from `cadence_day`/`cadence_week` relative to today instead. When given, it's validated against the rest of the cadence: it must fall on `cadence_day` (weekly/biweekly), or be the exact `cadence_week`'th occurrence of `cadence_day` in its month (monthly) — rejected with a specific message otherwise. Existing beats migrate to `weekly` with no day, so the due list shows them under "Needs setup" until set — a beat already on `monthly` before `cadence_week` existed, or on `biweekly` (nee `fortnightly`) before it required a day, is likewise left blank (not auto-guessed) and falls into "Needs setup" until the distributor picks one.
  - Lifecycle management (web-only, distributor-only, via `manage_beats` permission): create/edit-salesman/delete at `/manage/beats*`. `name` is the immutable primary key — "edit" reassigns the `salesman` column only. Delete is a hard delete, blocked when the beat is referenced in `vouchers.beat` or `completed_vouchers.beat` (current or historical — this single check also covers a beat with an active staging report, since its vouchers still live in the `vouchers` table).

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
  - headers: `bill_no,date,amount,salesman,created_by,created_at,payment_type,payment_ref`
  - notes:
    - `bill_no` refers to the voucher primary key.
    - `date` collection date, format yyyymmdd
    - `salesman`: Salesman name recording the payment.
    - `created_by`: login creating the record
    = `created_at`: time of record creation
    - `payment_type` (additive column, iteration5, web-only — see
      `checks.csv` below): `cash` (default) | `upi` | `check` | `returns`
      (retailer returned stock in lieu of money; `returns` added after
      iteration5 — the DB CHECK on `installments`/`completed_installments`
      is widened by the self-detecting `_migrate_payment_type_returns`).
      The CLI never sets this; every CLI-recorded (and pre-iteration5)
      installment is implicitly `cash`.
    - `payment_ref` (additive column, iteration5): free-form JSON, populated
      for `upi` (`{"txn_id": "..."}`) and `returns`
      (`{"items": [{"item", "qty", "price", "amount"}, ...]}` — item name
      <= 50 chars, whole-number qty, 2dp price, `amount` = qty x price, and
      the installment `amount` equals the sum of the items); empty for
      `cash`/`check` — check detail lives in `checks.csv` instead, not
      duplicated here.

- `checks.csv` (master data, added iteration5 — Payment Type Tracking + Check
  Lifecycle)
  - headers: `bill_no,beat,salesman,bank,branch,check_no,check_date,amount,
    status,recorded_by,recorded_at,resolved_by,resolved_at,resolution_note`
    (the SQLite table additionally has an autoincrement `id` primary key and
    an audit-only `installment_id` pointer — see below).
  - notes:
    - One row per check payment, created when a `coll-post` writes an
      installment whose `payment_type = 'check'` (web-submitted only — the
      CLI never creates check rows).
    - `check_date`: the date on the check; also the encashment due date used
      by the due-soon/overdue reminders.
    - `status`: `pending` (default) | `encashed` | `bounced`. `bounced` is
      terminal — there is no re-open action. "Due in 2 days" and "overdue"
      are **not** stored states; they are derived at query time from
      `status='pending'` and `check_date` vs today
      (`coll_store.check_summary_counts`/`load_checks`).
    - Marking a check `bounced` deletes its underlying installment row and
      recomputes the voucher balance from scratch (the same
      amount-minus-sum-of-installments invariant an `installment_delete`
      correction uses) — the money was never actually received. If the
      voucher had already been archived to `completed_vouchers` (fully
      settled by this very check), the balance is left untouched and a note
      is appended explaining why; un-archiving it is out of scope.
    - Like `vouchers.csv`/`installments.csv`, this file is the schema source
      of truth and first-run seed only — the SQLite `checks` table is
      authoritative once the app is running, and this CSV is not rewritten
      afterward.
  - Permission keys: `view_checks` (salesman, supervisor, distributor — the
    menu due/overdue/bounced banner and the Checks screen; nobody is
    excluded, since the reminder is informational for every role);
    `resolve_check` (distributor only — Mark Encashed / Mark Bounced).

**Validation Rules**
- Field-level: required fields present, types correct, lengths within sane limits.
- Referential: `beat` must exist in `beats.csv`; installment `bill_no` must refer to an existing voucher `bill_no` in `data` or staging during merge.
- Business logic: `installment.amount` must not exceed the voucher balance at merge time. `balance` is derived and stored on the voucher for easier lookup.
- Date windows: `date` values must be valid ISO `YYYY-MM-DD` and not in the future (enforced at add-vouchers validation and at coll approve/post).

**Iteration 1 Scope**
- Focus: schema confirmation and test-data generation only.
- Test-data generation CLI is separate from the core collection workflow and will be defined in later iterations.