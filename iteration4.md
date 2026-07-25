# iteration4 — Voucher Amendment (Web-Only Distributor Raw Editor)

*Plan drafted 2026-07-19. Keep this file updated if scope shifts during implementation; flag deviations before implementing them.*

**Status: IMPLEMENTED (2026-07-25).** Planned 2026-07-19; decision 4 revised 2026-07-25 (hard gate via redirect, replacing the original allow-with-banner call — see that section). The `amendments` table was explicitly approved before Phase 1. Phases 1-6 are built and tested (`python -m unittest` green); sandbox verification pending.

## Context

The correction-request machinery (iteration3) fixes one field at a time through a raise→review→apply lifecycle. It cannot express a multi-field fix (wrong date AND wrong salesman AND two bad installments), and the distributor — the master-data owner — has no direct editor at all. **Voucher Amendment** gives the distributor a raw, single-voucher editor: all voucher fields (`date`, `amount`, `beat`, `salesman`; `bill_no` immutable PK) plus full control of that voucher's installments (edit `date`/`amount`/`salesman`, delete rows, add rows), submitted as ONE atomic transaction that commits only if field validation, referential checks, an optimistic-concurrency snapshot check, and the balance reconciliation (`amount − SUM(installments) ≥ 0`) all pass. Every applied amendment is recorded with before/after snapshots.

**User decisions (2026-07-19):** name = "Voucher Amendment" (menu "Amend Voucher"); scope = one voucher at a time, all its fields + its installments (no add/delete voucher); bills in active staging reports stay editable (rely on existing approve/post revalidation + Return); web-only (CLI ignores it); audit every applied edit.

UI naming (CLAUDE.md): menu card **"Amend Voucher"**, screen header **"Voucher Amendment"** (bill_no on a meta line, not in the header — no technical prefixes).

## Design decisions

### 1. Audit storage: NEW `amendments` table — ⚠ schema change, needs explicit user approval before coding

Reusing `corrections` fails on: lifecycle mismatch (`status` drives approval-blocking + active-list logic — an instantly-`applied` amendment would leak "Correction applied" badges onto review screens); granularity mismatch (one amendment = voucher + N edits + M adds + K deletes → would shred into N+M+K+1 rows and need another kind-CHECK rebuild); column mismatch (request/resolve fields meaningless for self-applied edits).

```sql
CREATE TABLE IF NOT EXISTS amendments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no     TEXT NOT NULL CHECK (bill_no <> ''),
    old_json    TEXT NOT NULL,   -- full before-state: {"voucher": {...}, "installments": [...]}
    new_json    TEXT NOT NULL,   -- full after-state, incl. recomputed balance + new installment ids
    note        TEXT NOT NULL DEFAULT '',
    amended_by  TEXT NOT NULL,
    amended_at  TEXT NOT NULL
);
```

No status column — a row exists iff the amendment committed (written inside the same transaction as the master change). Not in `_TABLE_DDL_V1`; appended to `_SCHEMA` via `CREATE TABLE IF NOT EXISTS` like corrections, so installed DBs pick it up at next `init_db()`.

### 2. Payload model: full-state replace (not per-field diff)

The form IS the full state of one voucher. Store function is a compare-and-swap on:
- **snapshot** (hidden JSON field, exactly what was loaded): `{"voucher": {date,amount,balance,beat,salesman}, "installments": [{id,date,amount,salesman},...]}`
- **new_state** (parsed from form): same shape; installment `id` present → keep+UPDATE; `id` None → INSERT (`created_by=amended_by`); snapshot id absent from new_state → DELETE. Identity is by `id` throughout (duplicate rows are legal, `id` is the only identity — coll_store.py:560-564). Balance is never submitted; always recomputed.

### 3. Concurrency: `AmendmentConflict(ValueError)` mirroring `CorrectionConflict` (coll_store.py:791)

Inside the transaction, re-SELECT voucher + all installments; any drift from snapshot (voucher fields, installment id-set, or any row's fields — string compare, values round-trip verbatim DB→form→server) → `AmendmentConflict`, nothing commits, form re-renders with a reload prompt.

### 4. Open corrections on the bill: HARD GATE via redirect to the existing review page — ***revised 2026-07-25***

Superseded the original "allow + amber banner" call: the distributor must resolve every open **master-data** correction on the bill (`installment_amount`/`installment_delete`/`installment_add`/`voucher_amount`) before the raw editor is reachable at all. An open `collection_amount` request does **not** gate — it concerns only the current cycle's staged payment, and decision 7 never refreshes staged `payment` from an amendment, so an amendment structurally cannot invalidate it.

No new UI. `GET /coll/amend/{bill_no}` loads `open_corrections_for_bills([bill_no])`, filters to `_MASTER_KIND_LABELS` keys, and if any remain (oldest first — `open_corrections_for_bills` already returns `ORDER BY id`) redirects straight to the *existing* `coll/correction_review.html` page: `/coll/corrections/{cid}?next=/coll/amend/{bill_no}`. Only once none remain does the route render the real editor. Distributor resolves there (Apply/Reject, unchanged logic) → confirmation page → Back (now pointed at `next` instead of the hardcoded `/coll/corrections`) → lands back on the amend URL → gate re-evaluates. `amend_form.html` never needs to know corrections exist; all the logic is one guard clause in one route. See Phase 4 for the three small `next`-param threads this needs in the existing correction routes/template — no new template, no gated-mode rendering.

**Implementation-time note, not a new decision:** `apply_installment_correction` (existing) and `apply_voucher_amendment` (Phase 1, below) do the same shape of transaction — re-check a snapshot, mutate voucher/installment rows, recompute the balance, refuse on negative, write an audit row. Worth factoring their snapshot-check/mutate/recompute core into one shared primitive both call (a correction's single-field change is a degenerate case of an amendment's full-state patch) so validation rules and recompute policy live in one place. Pure internal reuse — no schema/table/behavior impact — so it's the Phase 1 implementer's call, not something requiring separate sign-off.

### 5. Single submit + live client-side balance preview (no 2-step confirm)

Server confirm step would widen the load→commit window (more conflicts) for no gain. Page-local JS (pattern: `templates/coll/submit_edit.html:62-166` live counter) recomputes `amount − Σ(non-deleted installments)` on input, red + client-side submit disable when negative (server check stays authoritative); `confirm()` on submit.

### 6. History surfaces: per-voucher section on the form + flat `GET /coll/amendments` list → `GET /coll/amendments/{id}` old-vs-new detail (layout: `correction_review.html`). Linked from the amend pick screen, NOT a menu card.

### 7. Staging interplay: generalize the staged refresh

Real gap verified: `_refresh_staged_balances` (coll_orchestrate.py:579) refreshes only `balance` (:601), but posting writes the **staged** `v["salesman"]` into new installment rows (`_append_installments`, coll_store.py:680) — a mid-flight salesman amendment would post attributed to the OLD salesman. Fix: rename to `_refresh_staged_voucher_fields(bill_no)`, copying `balance`, `date`, `salesman` from master into staged voucher dicts (same best-effort/display contract, same TXT regen). **Deliberately NOT `beat`** — the report's identity is its beat selection (beat lock, TXT header); beat changes apply from the next generated list. Update the corrections call site (:757 area) — behavior there unchanged.

---

# Phase 1 — `scripts/coll_store.py`

*Gate: explicit user approval for the `amendments` table before writing code (CLAUDE.md rule).*

1. `_AMENDMENTS_BODY` constant next to `_CORRECTIONS_BODY` (:163); append to `_SCHEMA`; same "NOT in `_TABLE_DDL_V1`" comment rationale.
2. `_backfill_amendment_permission(conn)` — `INSERT OR IGNORE ... ('distributor','amend_voucher')` — mirror `_backfill_coll_print_permission`, call from `init_db()`.
3. `class AmendmentConflict(ValueError)` next to `CorrectionConflict` (:791).
4. **`apply_voucher_amendment(bill_no, snapshot, new_state, amended_by, note="", now=None) -> dict`** — template: `apply_installment_correction` (:956-1031). One `with conn:` transaction:
   1. Load voucher; missing → `ValueError` (covers archived-mid-edit).
   2. Snapshot check (decision 3) → `AmendmentConflict` on drift.
   3. Validation (last-gate; API pre-validates for friendly errors): all amounts strict finite positive Decimal quantized 2dp (approach: `_parse_payment_strict` :643; GLOB CHECKs backstop); `beat` exists in `beats` (vouchers.beat has NO FK — this is the only referential guard); voucher + installment `salesman` exist in `users` with role `salesman`; dates non-empty (future-date rejection stays at API via `_valid_past_date`).
   4. Mutate: UPDATE vouchers fields; DELETE removed installments (`WHERE id IN (...) AND bill_no=?`); UPDATE kept rows (`created_by/created_at` untouched); INSERT new rows capturing `lastrowid`s.
   5. Reconcile: `_recompute_voucher_balance(conn, bill_no)` (:934) — negative → `ValueError`, full rollback (zero OK, same policy as corrections).
   6. Audit: INSERT `amendments` row in same transaction (`old_json` = verified snapshot, `new_json` = final state incl. recomputed balance + real new ids).
   7. Return amendment dict for the success message.
5. Readers: `load_amendments(bill_no=None, limit=None)` (most recent first) + `load_amendment(aid)` + `_amendment_dict`, mirroring `load_corrections`/`load_correction` (:832-864).

# Phase 2 — `scripts/coll_orchestrate.py`

1. Generalize `_refresh_staged_balances` (:579) → `_refresh_staged_voucher_fields(bill_no)` per decision 7; update corrections call site + docstring (beat caveat).
2. `amend_voucher(bill_no, snapshot, new_state, amended_by, note="") -> dict` — thin wrapper: store apply, then `_refresh_staged_voucher_fields(bill_no)`. No stage-based block (same rationale documented at `apply_correction_request` — `validate_staged_report` reads CURRENT master; Return is the remedy).

# Phase 3 — Permissions

`data/permissions.csv`: one row `distributor,amend_voucher` (single key gates menu card + all routes + history screens). DB backfill in Phase 1.

# Phase 4 — `scripts/coll_api.py` + templates

1. Routes (all `_require(request, "amend_voucher")` :137):
   - `GET /coll/amend` — bill_no search box (pattern: reports search) + "Amendment History" link. No `?correct=`-style threading; optional later: "Amend" button on standalone `voucher.html` (NOT `_voucher_inline.html`).
   - `GET /coll/amend/{bill_no}` — resolve via `search_voucher`; completed voucher → "Completed vouchers cannot be amended." (mirror `_load_correction_target` :985-995). **Correction gate (decision 4):** `open_corrections_for_bills([bill_no])` filtered to `_MASTER_KIND_LABELS`; non-empty → `_r(f"/coll/corrections/{corr['id']}?next=/coll/amend/{quote(bill_no, safe='')}")` for the oldest one, nothing rendered. Empty → render `coll/amend_form.html` with voucher, installments (ids), `load_beats()`/`load_salesmen()` dropdowns, `snapshot_json` hidden field, per-voucher history. No banner needed — the editor is only ever reached gate-clear.
   - `POST /coll/amend/{bill_no}` — `async`, `form = await request.form()` + `getlist` (fixed-arity `Form(...)` can't take variable rows). **Row encoding**: token `k` per row (existing rows: installment id; new rows: `new1…` via JS); fields `inst_date_{k}`, `inst_amount_{k}`, `inst_salesman_{k}`, `inst_delete_{k}`; ordered token list from `getlist("inst_row")` — no parallel-array misalignment. Validation ladder re-renders with SUBMITTED values + error: snapshot parse, dates via `_valid_past_date` (:944), amounts via `_valid_amount` (:930), beat/salesman membership, then `amend_voucher(...)`. `AmendmentConflict` → "This voucher changed while you were editing — [Reload current data]…"; `ValueError` → message. Success → message page "Amendment applied — new balance {balance}."
   - `GET /coll/amendments` + `GET /coll/amendments/{aid}` — history list + before/after detail.
   - **Existing correction routes gain a `next` passthrough (decision 4):** `GET /coll/corrections/{cid}` accepts `next: str = ""`, computes `back = _safe_from(next) if next else "/coll/corrections"`, passes to the template. `POST /coll/corrections/{cid}` accepts `next: str = Form(default="/coll/corrections")`, validates with `_safe_from`, uses it as `message.html`'s `back=`.
2. Templates: `coll/amend_form.html` (voucher fieldset; installments as 4-visible-column `data-table` grid: Date, Amount, Salesman, Delete — under the position-6 mobile nth-child limit; "Add Installment" + `<template>` row cloned by page JS; live balance summary; note; confirm()), `coll/amend_pick.html`, `coll/amendments.html`, `coll/amendment_review.html`; `templates/menu.html` card `{% if "amend_voucher" in perms %}` after Correction Requests. `templates/coll/correction_review.html`: both existing "Back" links become `href="{{ back }}"`, and the Apply/Reject form gains `<input type="hidden" name="next" value="{{ back }}">` — no new template.
3. CSS: stack-table mappings for the 4-col grid @640px; `.balance-preview.negative` red state; reuse existing alert/btn/field classes.

# Phase 5 — Tests

- `tests/test_coll_store.py` `TestAmendments(StoreTestCase)` mirroring `TestCorrections` (:901+): table auto-created on pre-existing DB; permission backfill; happy paths (fields-only / edit / delete / add / everything-at-once — assert master rows, balance, audit row contents); duplicate-row identity (delete exactly one of two identical rows); `AmendmentConflict` per drift class (voucher drift, row edited/added/deleted out-of-band); **atomicity** — negative balance / bad amount / unknown beat / non-salesman all leave vouchers, installments, AND amendments untouched.
- `tests/test_coll_orchestrate.py` `TestAmendVoucher`: wrapper return; staged `balance`/`date`/`salesman` refreshed + TXT regen; staged `beat` NOT touched; no active report → no error; extend existing refresh tests for the rename.
- `tests/test_coll_api.py` `TestAmendVoucher(ApiTestCase)`: menu card distributor-only; 403s for other roles; form render (fields, ids, dropdowns, history); POST happy path incl. staged refresh; each validation failure preserves submitted values; conflict re-render; completed/unknown bill refused; combined add+delete+edit via token encoding; **correction gate**: open master-data correction on the bill → amend redirects to `/coll/corrections/{cid}?next=...`; resolving (Apply or Reject) returns to the amend URL via `next`; two open corrections → resolving one still redirects to the other; open `collection_amount`-only request does **not** gate (editor opens directly); **regression**: amend below staged payment → approve-submit still blocked by `validate_staged_report`. The old "raise correction → amend → Apply raises `CorrectionConflict`" scenario is no longer reachable for master-data kinds (gate closes that window) — not tested as a reachable path, covered instead by the narrower TOCTOU note in Risks.
- `tests/test_coll_api.py` `TestCorrections`: add a `next`-param round-trip case (GET with `next=`, POST carries it forward, `message.html` back-link reflects it); default (`next` omitted) behavior unchanged — existing tests keep passing as-is.

# Phase 6 — Docs

- `schema.md`: `amendments` table (no CSV counterpart, iteration4, explicit approval) + `amend_voucher` key.
- `CLAUDE.md`: feature paragraph (revalidation/Return safety story, beat-refresh caveat) + data-layout line.
- `pipeline.md`: note that amendments share the corrections mid-flight-edit model; beat changes apply from next generated list.

# Verification (end-to-end via `/verify` sandbox skill)

Seed beats/salesmen/roles + vouchers with installments incl. one duplicate pair. As distributor: amend every field type at once → success shows new balance; audit row in DB. Delete one of two duplicates → correct one gone. Force negative balance → refused, nothing changed. Two tabs, apply in one, submit other → conflict banner. Amend bill in active submitted report → staged balance/salesman/TXT updated; shrink amount below staged payment → approve fails → Return → revise → approve → post. Other roles: no card, URLs refused. Mobile 390px grid usable. `python -m unittest` green.

**Correction-gate verification (decision 4, revised):** raise a master-data correction (e.g. `voucher_amount`) on a bill → open Amend Voucher for that bill → redirected straight to that correction's review page → Reject with a note → Back returns to the amend URL → editor now unlocked → amend proceeds normally. Same but Apply instead → master data updates → editor unlocks with post-correction values as the starting snapshot. Two open master-data corrections on one bill → resolving one still redirects to the other; only unlocks once both are resolved. Raise a `collection_amount` correction on a bill mid Approve-Collections → Amend Voucher for that bill is NOT gated. Visiting `/coll/corrections/{cid}` directly (not via the amend redirect) still works exactly as before.

# Risks

- Hidden snapshot tampering: only disables the tamperer's own stale-check; distributor-only raw editor — accepted.
- Dynamic form parsing is the fragile part — contained by token-keyed naming + dedicated API tests.
- Concurrent amenders: second submit fails cleanly via snapshot check; no lock needed (SQLite transaction).
- In-flight staging edits: intentionally allowed; gates re-validate against CURRENT master + Return (existing, tested). Staged `beat` intentionally not refreshed — documented.
- Stale open corrections: **structurally prevented** for the four master-data kinds — the gate blocks the editor until they're resolved, so the old self-inflicted `CorrectionConflict` scenario can't occur through the amend UI anymore. A narrow TOCTOU race remains only if a *new* master-data correction is raised between the gate check succeeding and the amend POST committing — same accepted-risk posture as snapshot-conflict handling elsewhere (surfaces as `AmendmentConflict` or a freshly-open correction on the next visit, no data corruption). `collection_amount` requests may still be open during an amend by design — an amend cannot invalidate them.
- String-equality snapshot compare: safe (verbatim round-trip; corrections precedent :984/:1014); form must never reformat hidden snapshot values.
- `bill_no` immutability: structural (URL path segment, never an input).
- Table grows unbounded: accepted (same posture as corrections).

# Implementation order

**Approval gate** (amendments table DDL + decisions 4/5/6) → Phase 1 + store tests → Phase 2 + orchestrate tests → Phases 3–4 + API tests → Phase 6 docs → sandbox verification. Each phase lands with its tests green before the next.
