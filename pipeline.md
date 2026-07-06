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

`(own beat)` / `(own)` are enforced server-side, not just hidden in the UI: a salesman is
restricted to beats they're assigned (`beats.salesman` column) and to reports whose
`selection[1]` (salesman) matches their own username. See roadmap.md's alpha milestone.

---

## Beat lock rule

Only **one active staging report per beat** is allowed at any time.  
A beat appears non-numbered in Generate Collection List as long as its report is in states 1–6.  
It becomes available again only after the report reaches state 7 (archived) or is cancelled.
