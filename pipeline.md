# Collection Pipeline — State Reference

Each collection report is a `coll*.json` file in `staging/` (or `archive/` once finalized).
Its state is encoded entirely in the `stages` sub-dict — the canonical single source of truth.

---

## State diagram

```
[Salesman]       [Supervisor/Dist.]    [Salesman]    [Supervisor/Dist.]   [Distributor]
     |                   |                   |                 |                  |
  coll-start         confirm-start       coll-submit      confirm-submit      coll-finalize
     |                   |                   |                 |                  |
     v                   v                   v                 v                  v

  ┌─────────┐       ┌─────────┐       ┌──────────┐      ┌──────────┐       ┌──────────┐
  │  START  │──────▶│  START  │──────▶│  SUBMIT  │─────▶│  SUBMIT  │──────▶│ FINALIZE │──▶ archived
  │   new   │  (y)  │confirmed│       │inprogress│      │confirmed │       │confirmed │
  └─────────┘       └─────────┘       └──────────┘      └──────────┘       └──────────┘
                                            │
                                     (all paid, submit)
                                            │
                                      ┌──────────┐
                                      │  SUBMIT  │
                                      │submitted │
                                      └──────────┘
                                            │
                                   [Supervisor/Dist.]
                                      confirm-submit
                                            │
                                            ▼
                                      ┌──────────┐
                                      │  SUBMIT  │
                                      │confirmed │
                                      └──────────┘
```

---

## States

### 1. START / new
> Report generated, awaiting confirmation.

| Field     | Value                              |
|-----------|------------------------------------|
| `stages`  | `{"start": "new"}`                 |
| Location  | `staging/`                         |
| UI label  | `[awaiting confirmation]`          |

**Actor:** Salesman, supervisor, or distributor via **coll-start**.  
**Next:** Supervisor or distributor confirms (`y`) → START / confirmed, or discards (`d`) → file deleted.

---

### 2. START / confirmed
> Supervisor or distributor approved the voucher list. Salesman may now submit payments.

| Field     | Value                              |
|-----------|------------------------------------|
| `stages`  | `{"start": "confirmed"}`           |
| Location  | `staging/`                         |
| UI label  | `[start confirmed]`                |

**Actor:** Supervisor or distributor via **coll-start → y** or **coll-confirm-start**.  
**Next:** Salesman opens **coll-submit** → SUBMIT / inprogress.

---

### 3. SUBMIT / inprogress
> Salesman has started entering payments but saved mid-way and exited.

| Field     | Value                                              |
|-----------|----------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "inprogress"}`   |
| Location  | `staging/`                                         |
| UI label  | `[submit in progress]`                             |

**Actor:** Salesman saved bookmark via **coll-submit → quit with save**.  
**Next:** Salesman reopens **coll-submit** to continue → SUBMIT / submitted.

---

### 4. SUBMIT / submitted
> Salesman visited all vouchers and submitted for supervisor review.

| Field     | Value                                              |
|-----------|----------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "submitted"}`    |
| Location  | `staging/`                                         |
| UI label  | `[submit in progress]`                             |

**Actor:** Salesman via **coll-submit → complete all → submit**.  
**Next:** Supervisor or distributor confirms via **coll-confirm-submit** → SUBMIT / confirmed.

---

### 5. SUBMIT / confirmed
> Supervisor or distributor approved the payment submission. Ready for distributor to finalize.

| Field     | Value                                              |
|-----------|----------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "confirmed"}`    |
| Location  | `staging/`                                         |
| UI label  | `[submit confirmed]`                               |

**Actor:** Supervisor or distributor via **coll-confirm-submit**.  
**Next:** Distributor finalizes via **coll-finalize** → FINALIZE / confirmed → archived.

---

### 6. FINALIZE / confirmed  *(terminal)*
> Payments written to `vouchers.csv` and `installments.csv`. File moved to `archive/`.

| Field     | Value                                                                    |
|-----------|--------------------------------------------------------------------------|
| `stages`  | `{"start": "confirmed", "submit": "confirmed", "finalize": "confirmed"}` |
| Location  | `archive/`  (moved from `staging/`)                                      |
| UI label  | *(not shown — no longer in staging)*                                     |

**Actor:** Distributor via **coll-finalize**.  
**Next:** None. Completed vouchers with zero balance are moved to `completed_vouchers.csv`.

---

## RBAC per action

| Action              | Salesman | Supervisor | Distributor |
|---------------------|:--------:|:----------:|:-----------:|
| coll-start          | ✓        | ✓          | ✓           |
| coll-confirm-start  |          | ✓          | ✓           |
| coll-submit         | ✓        | ✓          | ✓           |
| coll-confirm-submit |          | ✓          | ✓           |
| coll-finalize       |          |            | ✓           |

---

## Beat lock rule

Only **one active staging report per beat** is allowed at any time.  
A beat appears non-numbered in coll-start as long as its report is in states 1–5.  
It becomes available again only after the report reaches state 6 (archived).
