# collmgm Roadmap

## Released

### iteration3 — Approval Verification + Correction Requests (web)

Adds a physical-voucher verification workflow and a correction-request system to the two supervisor approval screens, closing the gap between what's on paper and what's staged in the system. Web-only throughout — the CLI approve flows are unaffected.

- **Phase 1 — Approve Collection List verification:** per-voucher + bundle-count checkboxes cross-check the staged list against the physical vouchers pulled from the locker; installment history auto-expands per active voucher (accordion, check-to-advance); Approve is hard-gated until every checkbox is ticked.
- **Phase 2 — Correction requests (master data):** a supervisor raises a structured correction (`installment_amount` / `installment_delete` / `installment_add` / `voucher_amount`) with a note; the distributor applies it (atomic master-data change + balance recompute) or rejects it from a dedicated Correction Requests screen. An open request blocks that voucher's verification and the list's approval; resolved requests are kept as an audit trail.
- **Phase 3 — Correction requests (collection amount):** at Approve Collections, a narrower correction kind lets the supervisor or distributor fix only the collection amount entered this cycle — never master data. Alternatively, returning the report to the salesman auto-settles the request once the resubmitted amount matches what was asked for.
- **Phase 4 — Approve Collections verification:** the same verification checkboxes, mirrored on the evening cross-check of returned vouchers and collected amounts; the bundle-count check reads "returned vouchers reconciled against my notes" since a voucher paid off in full is legitimately not returned.

321 tests passing (up from 69 at the last README snapshot). Schema addition (`corrections` table): `schema.md`. State-machine documentation: `pipeline.md`.

---

### LAN Web App

Browser-based access over the local network — no client install, works on desktop and mobile. Delivered incrementally as part of the ongoing alpha build line (see the `build/alpha-*` tags) rather than as a separately named release.

**Stack as built** (two points differ from the original plan, noted below):

| Layer | Technology | Notes |
|---|---|---|
| Backend | FastAPI + Uvicorn | Cookie-session auth, role-based routes mirroring the CLI workflow |
| Frontend | Jinja2 templates + vanilla JS | No JS framework; a small amount of hand-written JS handles inline expand/accordion, live payment validation, and the verification screens above — **not HTMX**, as originally planned |
| PWA | `manifest.json` + service worker | Home-screen icon on Android/iOS |
| Database | SQLite (`sqlite3`, stdlib) | `data/collmgm.db`; CSVs remain the schema source of truth and first-run seed; versioned via `PRAGMA user_version` with additive/rebuild migrations in `coll_store.init_db()` |
| Windows Service | NSSM wraps Uvicorn | Installer-driven (`packaging/service_setup.bat`); firewall rule opens port 8100 on LAN profiles |
| Client | Chrome (Android) / Safari (iOS) | "Add to Home Screen" once, tap icon forever |

- **Sub-milestone 1 (SQLite migration):** done — `coll_store.py` reads/writes `data/collmgm.db`; CSVs migrate in on first run.
- **Sub-milestone 2 (FastAPI + web UI):** done — `scripts/coll_api.py`, `templates/`, `static/`; mobile-responsive (stacked-card tables, sticky action bars) added in a later pass.
- **Sub-milestone 3 (Windows Service packaging):** done — `packaging/service_setup.bat` + `packaging/service_remove.bat`, wired into the Inno Setup installer (`packaging/setup.iss`).
- **Not implemented from the original plan:** `zeroconf`/`collmgm.local` mDNS hostname broadcasting — devices currently reach the server by hostname/IP and port 8100 directly.

---

### alpha — CLI with Login and RBAC
**Release tag:** `CollMgm-alpha-20260701230618`

Builds on beta0.1 and adds authenticated access with role-based workflow gates.

- Everything from beta0.1 (see below)
- Login prompt at startup; username/password validated against `users.csv`
- `current_user` (name, role) carried through the session
- Role-based access gates:
  - `coll-start`, `coll-submit`: salesman only, restricted to their assigned beat
  - `coll-approve-start`, `coll-approve-submit`: supervisor or distributor
  - `coll-post`: distributor only
  - Reports: all roles (read-only)

---

### beta0.1 — Collection workflow (CLI)
- Three-stage pipeline: `coll-start` → `coll-submit` → `coll-post`
- Beat-level pipeline guard: blocks duplicate collections for a beat already in staging
- Modular architecture: `coll_store` / `coll_data` / `coll_cli` / `coll_workflow`
- Report generation (JSON + TXT) with staging and archive lifecycle
- Batch balance update to `vouchers.csv` on post

---

## Planned

### iteration4 — Voucher Amendment (parked)

A raw, single-voucher editor for the distributor: all voucher fields plus full control of that voucher's installments, submitted as one atomic transaction with an audit trail (new `amendments` table). Complements iteration3's one-field-at-a-time correction requests for cases needing a multi-field fix. **Status: planned and designed, implementation not started** — needs explicit approval for the `amendments` schema change before coding begins. Full design: `iteration4.md`.
