# collmgm Roadmap

## Planned — maintenance backlog

Small, deliberately-deferred items from a codebase audit (see history for the full findings) — not urgent, but worth tracking instead of losing.

- **CI**: no automated test run exists anywhere — no `.github/workflows/`, no `test` target in the `Makefile`. Add a GitHub Actions workflow running `python -m unittest discover -s tests` on push/PR, and a matching `test` Makefile target for local use. Zero risk — pure addition, touches no app code.
- **Template/JS duplication**: `templates/coll/correction_form.html`, `new_vouchers_approve_item.html`, and `new_vouchers_review_item.html` each reimplement the same "pick a correction kind → reveal matching fields" UI (own copy-pasted `<script>`, near-identical markup) with small field-name drift between copies (`requested_by/requested_at` vs `raised_by/raised_at/edited_by`). `static/voucher_card.js` and `static/voucher_detail.js` separately duplicate their click-guard, fetch/session-expiry handling, and hidden/`aria-expanded` toggling. Consolidating either is reasonable long-term but needs manual browser verification (no JS test runner in this repo) — do as its own dedicated pass, not bundled with anything else.
- **Manual version bump**: `packaging/setup.iss`'s `MyAppVersion` is hand-edited per release (`packaging/BUILD_STEPS.md` step 3 of "To Update for the Next Release"). Worth scripting once the release process is touched again.

## Released

### CLI deprecated

The CLI (`collmenu.py`/`coll_cli.py`/`coll_workflow.py`) is deprecated: `packaging/setup.iss` no longer installs a shortcut for it, so new installs are web-app-only. Its code and tests remain in the repo and still work if launched directly (`run.bat`) — this is a hide, not a removal — but it gets no further feature work. See **CLI deprecation** in `CLAUDE.md`. The beta0.1/alpha entries below are kept as history.

---

### iteration4 — Voucher Amendment (web)

A raw, single-voucher editor for the distributor ("Amend Voucher"): all voucher fields plus full control of that voucher's installments (edit/delete/add), submitted as one atomic transaction with a load-time snapshot check (`AmendmentConflict`) and balance recompute. Complements iteration3's one-field-at-a-time correction requests for cases needing a multi-field fix. No approval step — every applied edit is recorded permanently in the new `amendments` table.

- **Gate:** the editor refuses to open while any open master-data correction exists on the bill — the distributor is redirected to that correction's review page to resolve it first, since an amendment could otherwise invalidate its snapshot; an open `collection_amount` request doesn't gate.
- **Staged interplay:** amending a voucher mid-flight in an active staging report is allowed (no stage-based block) — approve-submit/post always re-validate against CURRENT master data, and Return is the existing remedy. Applied amendments refresh the staged `balance`/`voucher_date`/`salesman` display fields, deliberately never `beat`.

361 tests passing (up from 321 at the last roadmap snapshot). Schema addition (`amendments` table): `schema.md`. State-machine documentation: `pipeline.md`.

---

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
