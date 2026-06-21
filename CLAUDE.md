# CLAUDE.md — collmgm

## Project snapshot

`collmgm` is a Windows CLI collection-management tool backed entirely by CSV files. It is built iteratively; **beta0.1 is the current released state** — the three-stage collection pipeline is complete and working.

Run the app: `run.bat` from the project root (launches `scripts/collmenu.py`).

---

## Architecture

```
collmenu.py          ← entry point / menu loop
    └── coll_workflow.py   ← orchestration (no I/O side-effects, calls ui + store + data)
            ├── coll_ui.py       ← all print()/input() calls live here
            ├── coll_data.py     ← query layer: reads CSVs + staging JSON, pure logic
            └── coll_store.py    ← persistence layer: paths, CSV/JSON reads and writes
```

### Module contracts

| Module | Responsibility | Must NOT |
|---|---|---|
| `coll_store.py` | All path constants, CSV/JSON reads/writes | print(), input(), import other coll_* modules |
| `coll_data.py` | Load and query master data; build report structures | print(), input() |
| `coll_ui.py` | All terminal I/O: prompts, display, editing | direct file I/O, business logic |
| `coll_workflow.py` | Orchestrate steps; enforce pipeline guards | own file paths (use coll_store), own terminal I/O (use coll_ui) |
| `collmenu.py` | Menu loop only | business logic |

The layering is strict: `coll_store` has no upstream deps; `coll_data` imports only from `coll_store`; `coll_ui` is standalone; `coll_workflow` imports all three.

---

## Collection pipeline (beta0.1)

Three sequential stages per beat:

1. **coll-start** (`run_coll_start`) — select beat + salesman → generate voucher list report → write `staging/collYYYYMMDD-<beat>-<salesman>.json` + `.txt`. Stage = `step1`, all `isconfirmed = false`.
2. **coll-submit** (`run_coll_submit`) — pick a `step1`-confirmed report → enter payment amounts in edit mode → save payments to staging JSON. Advances stage to `step2`.
3. **coll-finalize** (`run_coll_finalize`) — pick a `step2`-confirmed report → batch-update `data/vouchers.csv` balances + `data/installments.csv` → move report to `archive/`.

**Beat-level pipeline guard:** only one active staging report per beat is allowed. A second `coll-start` for the same beat is blocked until the existing report is finalized or removed.

### Report JSON schema (staging)

```json
{
  "beat": "beat1",
  "salesmen": ["saleman1"],
  "date": "2026-06-20",
  "stage": "step1",
  "confirmations": [
    {"stage": "step1", "isconfirmed": false},
    {"stage": "step2", "isconfirmed": false},
    {"stage": "step3", "isconfirmed": false}
  ],
  "vouchers": [
    {"bill_no": "...", "voucher_date": "...", "balance": "100.00", "payment": "", "beat": "beat1", "salesman": "saleman1"}
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

## Development principles

- **Minimal dependencies:** standard Python only — `csv`, `json`, `decimal`, `pathlib`, `datetime`. No third-party packages in core scripts.
- **Iterative:** each iteration has a plan file (`iteration2.md`). Stick to plan scope; flag any deviation before implementing.
- **No schema changes without explicit approval.** The CSV schemas in `schema.md` are the source of truth.
- **Layer discipline:** never move I/O calls into `coll_data`/`coll_store`; never put file paths in `coll_workflow` directly.
- **No float for money.** Always `Decimal`.

---

## Planned next milestones (see roadmap.md for detail)

1. **Login + RBAC** — authenticate at startup, gate `coll-start`/`coll-submit` to salesmen and `coll-finalize` to supervisor/distributor. `current_user` parameter stubs already exist in `coll_data.py`.
2. **REST API layer** — FastAPI façade over `coll_data`/`coll_store`; requires splitting `coll_workflow` into pure-logic functions (no `input()`/`print()`).
3. **Schema enhancement + SQLite migration** — add `password_hash`, `beat` ownership, customer fields; migrate storage backend at API milestone. `coll_store.py` is the clean seam for the backend swap.

---

## Key files for onboarding

| File | What to read first |
|---|---|
| `roadmap.md` | Current status and planned milestones |
| `schema.md` | Canonical CSV schemas and validation rules |
| `iteration2.md` | Detailed spec for the implemented pipeline |
| `agent.md` | Agent behavior guidelines (iterative development rules) |
| `scripts/coll_store.py` | All path constants and I/O primitives |
| `scripts/coll_data.py` | Data loading and query functions |
