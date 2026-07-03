# CLAUDE.md — collmgm

## Project snapshot

`collmgm` is a Windows CLI collection-management tool backed entirely by CSV files. It is built iteratively; **alpha (`CollMgm-alpha-20260701230618`) is the current released state** — the three-stage collection workflow, user login, and RBAC are all complete and working.

Run the app: `run.bat` from the project root (launches `scripts/collmenu.py`).

---

## Architecture

```
collmenu.py          ← entry point / menu loop
    └── coll_workflow.py   ← orchestration (no I/O side-effects, calls ui + store + data)
            ├── coll_cli.py       ← all print()/input() calls live here
            ├── coll_data.py     ← query layer: reads CSVs + staging JSON, pure logic
            └── coll_store.py    ← persistence layer: paths, CSV/JSON reads and writes
```

### Module contracts

| Module | Responsibility | Must NOT |
|---|---|---|
| `coll_store.py` | All path constants, CSV/JSON reads/writes | print(), input(), import other coll_* modules |
| `coll_data.py` | Load and query master data; build report structures | print(), input() |
| `coll_cli.py` | All terminal I/O: prompts, display, editing | direct file I/O, business logic |
| `coll_workflow.py` | Orchestrate steps; enforce workflow guards | own file paths (use coll_store), own terminal I/O (use coll_cli) |
| `collmenu.py` | Menu loop only | business logic |

The layering is strict: `coll_store` has no upstream deps; `coll_data` imports only from `coll_store`; `coll_cli` is standalone; `coll_workflow` imports all three.

---

## Collection workflow (beta0.1)

Five sequential steps per beat:

1. **coll-start** (`run_coll_start`) — select beat + salesman → generate voucher list → write `staging/coll*.json` + `.txt`. `stages.start = "new"`.
2. **coll-approve-start** (`run_coll_approve_start`) — supervisor approves the list → `stages.start = "confirmed"`. Supervisor may also Return (list deleted, salesman must regenerate) or Cancel.
3. **coll-submit** (`run_coll_submit`) — salesman enters payments → `stages.submit = "inprogress"` (mid-session) or `"submitted"` (all vouchers completed). Salesman may Cancel before editing begins.
4. **coll-approve-submit** (`run_coll_approve_submit`) — supervisor approves payments → `stages.submit = "confirmed"`. Supervisor may Return (→ `"returned"`, salesman must revise with prior payments intact).
5. **coll-post** (`run_coll_post`) — distributor writes to `data/vouchers.csv` + `data/installments.csv` → report archived. Distributor may Return (→ `"submitted"`, supervisor re-approves).

**Beat-level workflow guard:** only one active staging report per beat is allowed. A second `coll-start` for the same beat is blocked until the existing report is posted or cancelled.

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
  ]
}
```

---

## Data layout

```
data/
  users.csv           — name, role  (roles: distributor | supervisor | salesman | system)
  beats.csv           — name
  vouchers.csv        — bill_no, date, amount, balance, beat, salesman, created_by, created_at
  installments.csv    — bill_no, date, amount, salesman, created_by, created_at
  completed_vouchers.csv     — archived finalized vouchers
  completed_installments.csv — archived finalized installments

staging/              — active collection reports (JSON + TXT pairs)
archive/              — finalized collection reports (JSON + TXT pairs)
```

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
- "Confirm" is reserved for user acknowledgement prompts (e.g. "Keep this collection list? y/n"), not for supervisor sign-off actions

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

1. **REST API layer** — FastAPI façade over `coll_data`/`coll_store`; requires splitting `coll_workflow` into pure-logic functions (no `input()`/`print()`).
2. **Schema enhancement + SQLite migration** — add `beat` ownership, customer fields; migrate storage backend at API milestone. `coll_store.py` is the clean seam for the backend swap.

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
