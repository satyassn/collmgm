# CLAUDE.md — collmgm

## Project snapshot

`collmgm` is a Windows CLI collection-management tool backed entirely by CSV files. It is built iteratively; **alpha (`CollMgm-alpha-20260701230618`) is the current released state** — the three-stage collection workflow, user login, and RBAC are all complete and working.

Run the app: `run.bat` from the project root (launches `scripts/collmenu.py`).

---

## Architecture

```
collmenu.py          ← CLI entry point / menu loop
    └── coll_workflow.py   ← CLI prompt/display loops (calls coll_cli + coll_orchestrate)
coll_api.py           ← Web entry point (FastAPI routes; calls coll_orchestrate directly)
    └── coll_orchestrate.py ← shared pure stage-transition logic (used by BOTH coll_workflow.py and coll_api.py)
            ├── coll_cli.py       ← all print()/input() calls live here (coll_workflow only)
            ├── coll_data.py     ← query layer: reads CSVs + staging JSON, pure logic
            └── coll_store.py    ← persistence layer: paths, CSV/JSON reads and writes
```

### Module contracts

| Module | Responsibility | Must NOT |
|---|---|---|
| `coll_store.py` | All path constants, CSV/JSON reads/writes | print(), input(), import other coll_* modules |
| `coll_data.py` | Load and query master data; build report structures | print(), input() |
| `coll_orchestrate.py` | Shared, I/O-agnostic stage-transition logic for the 5-stage workflow (coll-start → coll-post), used by both `coll_workflow.py` and `coll_api.py` so the CLI and web app cannot drift | print(), input(), import `coll_cli` |
| `coll_cli.py` | All terminal I/O: prompts, display, editing | direct file I/O, business logic |
| `coll_workflow.py` | CLI orchestration loops; enforce workflow guards | own file paths (use coll_store), own terminal I/O (use coll_cli), duplicate logic that belongs in `coll_orchestrate.py` |
| `coll_api.py` | Web (FastAPI) routes; session/cookie auth, permission checks, template rendering | duplicate stage-transition logic (use `coll_orchestrate.py`) |
| `collmenu.py` | Menu loop only | business logic |

The layering is strict: `coll_store` has no upstream deps; `coll_data` imports only from `coll_store`; `coll_cli` is standalone; `coll_orchestrate` imports only `coll_store`/`coll_data` (never `coll_cli`); `coll_workflow` and `coll_api` both import `coll_orchestrate` (plus `coll_data`/`coll_store` for queries) — `coll_workflow` additionally imports `coll_cli`.

---

## Collection workflow (beta0.1)

Five sequential steps per beat:

1. **coll-start** (`run_coll_start`) — select beat + salesman → generate voucher list → write `staging/coll*.json` + `.txt`. `stages.start = "new"`.
2. **coll-approve-start** (`run_coll_approve_start`) — supervisor approves the list → `stages.start = "confirmed"`. Supervisor may also Return (list deleted, salesman must regenerate) or Cancel.
3. **coll-submit** (`run_coll_submit`) — salesman enters payments → `stages.submit = "inprogress"` (mid-session) or `"submitted"` (all vouchers completed). Salesman may Cancel before editing begins.
4. **coll-approve-submit** (`run_coll_approve_submit`) — supervisor approves payments → `stages.submit = "confirmed"`. Supervisor may Return (→ `"returned"`, salesman must revise with prior payments intact).
5. **coll-post** (`run_coll_post`) — distributor writes to `data/vouchers.csv` + `data/installments.csv` → report archived. Distributor may Return (→ `"submitted"`, supervisor re-approves).

**Beat-level workflow guard:** only one active staging report per beat is allowed. A second `coll-start` for the same beat is blocked until the existing report is posted or cancelled.

**Correction requests (web-only, iteration3):** during an approval review a supervisor may raise a structured correction (plus a free-text note). Requests live in the SQLite `corrections` table (see `schema.md`). Which kinds are raiseable is stage-restricted: the **Approve Collection List** review offers the four master-data kinds (`installment_amount` / `installment_delete` / `installment_add` / `voucher_amount` — resolved only by the distributor; Apply = atomic master change + balance recompute); the **Approve Collections** review offers only `collection_amount` (this cycle's staged payment — resolved by supervisor OR distributor via `coll_approve_submit`, or by Return-to-salesman, which auto-settles the request when the resubmitted payment matches). An **open** request blocks web approval of any staging report containing that `bill_no` (and disables that voucher's verification checkbox); **applied** requests stay listed on the Correction Requests screen — linking to the workflow they gate — until that report posts or is cancelled (derived at render time, nothing stored). The CLI ignores corrections entirely.

**Voucher Amendment (web-only, iteration4):** the distributor's raw, single-voucher editor ("Amend Voucher") — all voucher fields plus full control of that voucher's installments (edit/delete/add), submitted as one atomic transaction with an optimistic-concurrency snapshot check (`AmendmentConflict`) and balance reconciliation. Audited in the SQLite `amendments` table (see `schema.md`) — every applied edit is a permanent before/after record, no approval step. **Gate:** the editor refuses to open while any open master-data correction exists on the bill (redirects to that correction's review page instead, since an amendment could invalidate its snapshot); an open `collection_amount` request does not gate. **Revalidation/Return safety:** amending a voucher mid-flight in an active staging report is allowed — no stage-based block — because `validate_staged_report` and posting always re-check every payment against CURRENT master data, so an amendment that invalidates a staged payment surfaces there and is remedied by the existing Return flow, exactly like corrections. **Staged-display refresh caveat:** an applied amendment refreshes the staged `balance`/`voucher_date`/`salesman` display fields in any report that currently holds the bill, but deliberately **not** `beat` — the report's identity is its beat selection (beat lock, TXT header), so a beat change only takes effect starting with the next generated list. The CLI ignores amendments entirely.

**Amendment Requests (web-only, iteration4b):** a lightweight raise→resolve lifecycle in front of Voucher Amendment, so a supervisor or salesman can flag a bill_no for the distributor without holding `amend_voucher` themselves. Requests live in the SQLite `amendment_requests` table (see `schema.md`) — free-text note only, no structured kind/snapshot like `corrections` has, since the actual fix is left to the distributor's raw editor. **Not stage-gated:** raiseable any time via its own menu entry ("Request Voucher Amendment") or from any voucher lookup — the raw editor itself isn't tied to a staging report, so its request mechanism isn't either. **Cross-link:** the Correction Requests raise form also offers "Raise an Amendment Request instead" when none of the four structured kinds fit, but only in the Approve Collection List context (never Approve Collections, whose sole `collection_amount` kind is unrelated to a full voucher/installment edit) — this reuses the same `/coll/amend-request` route and table, not a new correction `kind`. **Resolution:** no "apply" action exists on the request itself — every open request on a bill is auto-marked `applied` (linked to the new row) the moment `amend_voucher()` lands any edit on that bill_no, regardless of whether the note matches the specific fields changed; the distributor may instead **reject** a request without amending, and the raiser may **withdraw** their own open request. No gating: unlike corrections, an open amendment request never blocks verification checkboxes, report approval, or opening the raw editor — it only surfaces as an informational banner there. Permission keys: `raise_amendment_request` (supervisor, salesman); `amend_voucher` (existing, distributor only) doubles as both the resolve and view permission. `raise_correction` was also widened to salesman as part of this change. The CLI ignores amendment requests entirely.

**Payment Type Tracking + Check Lifecycle (web-only, iteration5):** at Submit Collections each voucher's payment gets a `payment_type` — `cash` (default) | `upi` (requires a UPI transaction id) | `check` (requires bank, branch, check #, and check date) | `returns` (retailer returned stock instead of paying: an itemised list of item name ≤50 chars / whole-number quantity / 2dp price, with a per-line amount = qty × price; the **items total becomes the voucher's payment**, recomputed server-side and cross-checked at approve/post, and is settled at Post like any other payment; items are stored as JSON in `payment_ref`, no separate table) — captured only by the web submit form and carried through the staging JSON/installments sidecar, approve/post review screens, and finally into the DB `installments`/`completed_installments` tables (`payment_type`, `payment_ref` — additive columns, default `cash`/empty). The Submit/Approve Collections/Post Collections summary boxes show a cash/UPI/check count + amount breakdown (`coll_orchestrate.payment_type_totals`). **Checks are master data, not an audit-only table**: posting a check-type payment writes a row to the new `checks` table/`checks.csv` (see `schema.md`) with lifecycle `status` `pending` → `encashed` | `bounced`. "Due in 2 days" and "overdue" are derived at render time from `check_date` vs today, never stored. A small menu banner plus a dedicated **Checks** screen (`/coll/checks`) show due-soon/overdue/bounced counts to every role (`view_checks` permission — salesman, supervisor, distributor); only the distributor (`resolve_check`) can Mark Encashed (status flip, done) or Mark Bounced (deletes the underlying installment and recomputes the voucher balance from scratch in one transaction — the money was never actually received — unless the voucher was already archived by that very check, in which case the balance is left alone with an explanatory note; un-archiving is out of scope). The CLI ignores payment type and checks entirely — every CLI-recorded payment is implicitly `cash`.

### Report JSON schema (staging)

```json
{
  "selection_type": "beat_salesman",
  "selection": ["beat1", "salesman1"],
  "date": "2026-06-20",
  "stages": {
    "start":  "new | confirmed",
    "submit": " | inprogress | submitted | returned | confirmed",
    "post":   " | confirmed"
  },
  "vouchers": [
    {"bill_no": "...", "date": "...", "balance": "100.00", "payment": "", "payment_date": "", "beat": "beat1", "salesman": "salesman1"}
  ],
  "verification": {"bill_nos": ["..."], "count": true}
}
```

`verification` is optional, web-only bookkeeping for the physical-voucher checks on **both** the Approve Collection List and Approve Collections screens (per-voucher + count checkboxes) — same shape, reused because each stage's approval pops the key on exit. Present only while `stages.start == "new"` or `stages.submit == "submitted"`, respectively; `apply_start_approval` pops it on approve (every entry point) and `apply_submit_approval` pops it on **either** approve or return (a submit-stage return does not delete the file, so a stale state must not resurface on the resubmitted report's next review). The CLI ignores it. The Approve Collections count checkbox reads "Returned vouchers reconciled against my notes" — unlike the morning bundle count it does not assert the returned count equals the voucher count, since a missing voucher can legitimately mean the customer paid it off in full.

---

## Data layout

```
data/
  users.csv           — name, role  (roles: distributor | supervisor | salesman | system)
  beats.csv           — name
  vouchers.csv        — bill_no, date, amount, balance, beat, salesman, created_by, created_at
  installments.csv    — bill_no, date, amount, salesman, created_by, created_at, payment_type, payment_ref
  checks.csv           — check lifecycle master data (bank/branch/check#/date/amount/status) — see Payment Type Tracking below
  completed_vouchers.csv     — archived finalized vouchers
  completed_installments.csv — archived finalized installments

staging/              — active collection reports (JSON + TXT pairs)
archive/              — finalized collection reports (JSON + TXT pairs)
```

Master data actually lives in `data/collmgm.db` (SQLite; CSVs are the schema source of truth and first-run seed). The DB additionally holds `permissions`, the `corrections` table (correction requests + audit trail), the `amendments` table (Voucher Amendment audit trail), and the `amendment_requests` table (Amendment Requests raise→resolve lifecycle) — none of those four has a CSV counterpart; see `schema.md`. `checks` (iteration5) IS master data like `vouchers`/`installments` — it has a `checks.csv` counterpart, unlike the four SQLite-only tables above.

CSV conventions: comma delimiter, UTF-8, ISO 8601 dates, `Decimal` for amounts (never float).

`bill_no` is the voucher primary key. Installments reference it. `balance` is a derived stored value (total amount minus paid installments).

---

## UI naming conventions

These rules apply to all **user-facing strings** — menu labels, screen headers, document headers, and user prompts. Internal identifiers (function names, variable names, JSON keys, file prefixes) are unaffected.

| Term | Definition | Used for |
|---|---|---|
| **Collection List** | The working document generated at coll-start — the voucher list a salesman takes to the field | Menu labels, screen headers, TXT/HTML file headers |
| **Collection Report** | Analytical/summary views only | Reports sub-menu screens |
| **Approve** | Supervisor grants sign-off to advance a workflow stage | Replaces "Confirm" for all supervisor gatekeeping actions |
| **Post** | Distributor writes approved data to master CSV files | Replaces "Finalize" for write-to-master actions |
| **Return** | Approver sends a report one step back for correction | Approve Collection List, Approve Collections, Post Collections prompts |
| **Cancel** | Abandon a collection list where no prior work is lost | Generation screen, Approve Collection List, Submit Collections (salesman pre-edit) |

### Role-action pattern

```
Salesman generates / submits  →  Supervisor approves / returns  →  Distributor posts / returns
Cancel: salesman at generation, supervisor/distributor at approve-list, salesman at pre-edit submit
```

### Style rules

- Menu labels and screen headers: **Title Case**
- Screen headers must match the corresponding menu label exactly — no technical prefixes (e.g. no `coll-start - `)
- Document file headers (TXT/HTML): **ALL CAPS** (e.g. `COLLECTION LIST`)
- "Confirm" is reserved for user acknowledgement prompts (e.g. "Start this collection list? y/n"), not for supervisor sign-off actions

---

## Development principles

- **Minimal dependencies:** standard Python only — `csv`, `json`, `decimal`, `pathlib`, `datetime`. No third-party packages in core scripts.
- **Iterative:** each iteration has a plan file (`iteration2.md`). Stick to plan scope; flag any deviation before implementing.
- **No schema changes without explicit approval.** The CSV schemas in `schema.md` are the source of truth.
- **Layer discipline:** never move I/O calls into `coll_data`/`coll_store`; never put file paths in `coll_workflow` directly.
- **No float for money.** Always `Decimal`.

---

## Completed milestones

1. **beta0.1** — Three-stage collection workflow CLI (released).
2. **Login + RBAC** — Login at startup, role-based gates for all workflow steps (released in alpha).

## Planned next milestones (see roadmap.md for detail)

**LAN Web App** — browser-based access over the local network, PWA home-screen icon, no client install. Three sub-milestones in order:

1. **SQLite migration** — swap `coll_store.py` backend to SQLite using the existing schema unchanged. Required for concurrent LAN access. Schema enhancements deferred.
2. **FastAPI + HTMX web UI** — refactor `coll_workflow.py` into pure logic; add `coll_api.py` (FastAPI, cookie auth); add `templates/` (Jinja2 + HTMX, mobile-responsive, role-based); PWA manifest + `zeroconf` for `collmgm.local` hostname.
3. **Windows Service packaging** — NSSM wraps Uvicorn as auto-start service; firewall rule for LAN port; updated Inno Setup installer.

---

## Key files for onboarding

| File | What to read first |
|---|---|
| `roadmap.md` | Current status and planned milestones |
| `schema.md` | Canonical CSV schemas and validation rules |
| `pipeline.md` | Collection workflow state reference (stages, RBAC, state diagram) |
| `agent.md` | Agent behavior guidelines (iterative development rules) |
| `scripts/coll_store.py` | All path constants and I/O primitives |
| `scripts/coll_data.py` | Data loading and query functions |
