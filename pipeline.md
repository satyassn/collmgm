# Collection Workflow — State Reference

Each collection report is a `coll*.json` file in `staging/` (or `archive/` once finalized).
Its state is encoded entirely in the `stages` sub-dict — the canonical single source of truth.

---

## State diagram

```
[Salesman]    [Supervisor]   [Salesman]     [Supervisor]   [Distributor]
     |               |             |               |               |
  coll-start   approve-start  coll-submit   approve-submit   coll-post
     |               |             |               |               |
     v               v             v               v               v

┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  START   │──▶│  START   │──▶│  SUBMIT  │──▶│  SUBMIT  │──▶│   POST   │──▶ archived
│   new    │   │confirmed │   │inprogress│   │confirmed │   │confirmed │
└──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
  (cancel)      (r) or (c)      (cancel)           │ (r)          │ (r)
  during gen    file deleted     pre-edit           │              │
                                               ┌────▼─────┐       │
                                               │  SUBMIT  │◀──────┘
                                               │submitted │  submit="submitted"
                                               └──────────┘
                                                    │ (r)
                                               ┌────▼─────┐
                                               │  SUBMIT  │
                                               │ returned │  salesman edits or cancels
                                               └──────────┘
```

**Return chain:** Distributor returns → `submit="submitted"` (supervisor queue) → Supervisor returns → `submit="returned"` (salesman queue) → Salesman cancels or re-edits and resubmits.

---

## States

### 1. START / new
> Collection list generated, awaiting supervisor approval.

| Field     | Value                              |
|-----------|------------------------------------|
| `stages`  | `{"start": "new"}`                 |
| Location  | `staging/`                         |
| UI label  | `[awaiting approval]`              |

**Actor:** Salesman, supervisor, or distributor via **Generate Collection List**.  
**Next:** Supervisor approves (`y`) → START / confirmed; or Returns (`r`) → file deleted, salesman regenerates; or Cancels (`c`) → file deleted.

**Web-only verification gate:** on the web Approve Collection List screen the supervisor must tick a verification checkbox per voucher (cross-checked against the physical voucher) plus a bundle-count checkbox before Approve unlocks; state persists in a transient top-level `verification` key in the report JSON (enforced in `coll_api` only — the CLI approve flow is unchanged, and `apply_start_approval` pops the key on approve from either entry point).

**Correction loop (web-only):** when the physical cross-check reveals wrong master data, the supervisor raises a structured correction request from the voucher's expanded detail (edit/delete/add installment, or edit voucher amount — see `schema.md` `corrections` table). While a request is **open**: that voucher cannot be verified, and web approval of the whole list is blocked. The distributor resolves it from **Correction Requests** (menu): *Apply* changes master data + recomputes the balance atomically and refreshes the staged display balance; *Reject* (with note) unblocks the voucher unchanged. A correction applied while a report is mid-submit is safe by design — approve-submit and post re-validate payments against CURRENT master balance, and the existing Return actions are the remedy when a payment no longer fits.

**Voucher Amendment (web-only, iteration4):** the distributor's raw editor (**Amend Voucher**, menu) shares the same mid-flight-edit safety model as the correction loop above — amending a voucher that's sitting in an active staging report is allowed with no stage-based block, because the same CURRENT-master revalidation + Return remedy applies. It refreshes the same staged display fields a correction does (`balance`/`voucher_date`/`salesman`) in every report holding the bill, but never `beat` — beat changes apply starting with the next generated list, not to reports already in flight. **Gate:** the editor is unreachable while any open master-data correction exists on the bill — the distributor is redirected to resolve it first (Apply or Reject) — since amending could otherwise invalidate that correction's snapshot; an open `collection_amount` request doesn't gate (see `schema.md` `amendments` table).

---

### 2. START / confirmed
> Collection list approved. Salesman may now submit payments.

| Field     | Value                              |
|-----------|------------------------------------|
| `stages`  | `{"start": "confirmed"}`           |
| Location  | `staging/`                         |
| UI label  | `[start approved]`                 |

**Actor:** Supervisor or distributor via **Approve Collection List → y**.  
**Next:** Salesman opens **Submit Collections** → SUBMIT / inprogress.

---

### 3. SUBMIT / inprogress
> Salesman has started entering payments but saved mid-way and exited.

| Field     | Value                                              |
|-----------|----------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "inprogress"}`   |
| Location  | `staging/`                                         |
| UI label  | `[submit in progress]`                             |

**Actor:** Salesman saved bookmark via **Submit Collections → quit with save**.  
**Next:** Salesman reopens **Submit Collections** to continue → SUBMIT / submitted. Or Cancels (`c`) at pre-edit prompt → file deleted, beat released.

---

### 4. SUBMIT / submitted
> Salesman visited all vouchers and submitted for supervisor review.

| Field     | Value                                              |
|-----------|----------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "submitted"}`    |
| Location  | `staging/`                                         |
| UI label  | `[submit in progress]`                             |

**Actor:** Salesman via **Submit Collections → complete all → submit**.  
**Next:** Supervisor approves via **Approve Collections → y** → SUBMIT / confirmed; or Returns (`r`) → SUBMIT / returned.

**Web-only correction loop (this stage):** from the web Approve Collections review the supervisor may raise a correction against ONLY the collection amount entered this cycle (never the voucher amount or past installments — those kinds belong to the Approve Collection List stage). While open, it blocks web approval (Return stays available). Resolution: supervisor or distributor applies it (rewrites the staged payment + sidecar + TXT), rejects it, or the supervisor Returns the report — a resubmitted payment matching the requested value auto-settles the request ("matched after salesman revision"). The CLI is unaffected.

**Web-only verification gate (this stage):** mirrors the morning Approve Collection List screen — a per-voucher checkbox plus a "returned vouchers reconciled against my notes" checkbox must all be ticked before Approve unlocks. Gate order on approve: data-validity check (existing tamper defense) → open corrections → verification. Enforced in `coll_api` only; the CLI approve flow is unaffected. State is popped from the report JSON on either approve or return (a returned report is not deleted, so stale checkmarks must not resurface when the salesman resubmits).

---

### 5. SUBMIT / returned
> Supervisor returned the payment submission for correction. Salesman must revise and resubmit.

| Field     | Value                                               |
|-----------|-----------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "returned"}`      |
| Location  | `staging/`                                          |
| UI label  | `[return requested]`                                |

**Actor:** Supervisor via **Approve Collections → Return (r)**.  
**Next:** Salesman reopens **Submit Collections** → sees "RETURN REQUESTED" notice → prior payments loaded as defaults → edits → resubmits → SUBMIT / submitted. Or Cancels (`c`) at pre-edit prompt → file deleted, beat released.

---

### 6. SUBMIT / confirmed
> Supervisor approved the payment submission. Ready for distributor to post.

| Field     | Value                                              |
|-----------|----------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "confirmed"}`    |
| Location  | `staging/`                                         |
| UI label  | `[submit approved]`                                |

**Actor:** Supervisor or distributor via **Approve Collections → y**.  
**Next:** Distributor posts via **Post Collections → y** → POST / confirmed → archived. Or Returns (`r`) → SUBMIT / submitted (supervisor re-reviews).

---

### 7. POST / confirmed  *(terminal)*
> Payments written to `vouchers.csv` and `installments.csv`. File moved to `archive/`.

| Field     | Value                                                                    |
|-----------|--------------------------------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "confirmed", "post": "confirmed"}` |
| Location  | `archive/`  (moved from `staging/`)                                      |
| UI label  | *(not shown — no longer in staging)*                                     |

**Actor:** Distributor via **Post Collections → y**.  
**Next:** None. Completed vouchers with zero balance are moved to `completed_vouchers.csv`.

---

## RBAC per action

| Action                    | Salesman     | Supervisor | Distributor |
|---------------------------|:------------:|:----------:|:-----------:|
| Generate Collection List  | ✓ (own beat) | ✓          | ✓           |
| Approve Collection List   |              | ✓          | ✓           |
| Return Collection List    |              | ✓          | ✓           |
| Cancel Collection List    | ✓ (own)      | ✓          | ✓           |
| Submit Collections        | ✓ (own)      | ✓          | ✓           |
| Cancel Submission         | ✓ (own)      |            |             |
| Approve Collections       |              | ✓          | ✓           |
| Return Collections        |              | ✓          | ✓           |
| Post Collections          |              |            | ✓           |
| Return to Supervisor      |              |            | ✓           |
| Raise Correction (web)    |              | ✓          | ✓           |
| View Corrections (web)    |              | ✓          | ✓           |
| Apply/Reject Correction — master-data kinds (web) | | | ✓           |
| Apply/Reject Correction — collection amount (web) | | ✓ | ✓         |
| Amend Voucher (web)       |              |            | ✓           |

`(own beat)` / `(own)` are enforced server-side, not just hidden in the UI: a salesman is
restricted to beats they're assigned (`beats.salesman` column) and to reports whose
`selection[1]` (salesman) matches their own username. See roadmap.md's alpha milestone.

---

## Beat lock rule

Only **one active staging report per beat** is allowed at any time.  
A beat appears non-numbered in Generate Collection List as long as its report is in states 1–6.  
It becomes available again only after the report reaches state 7 (archived) or is cancelled.
