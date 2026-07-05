# collmgm Roadmap

## Released

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

### LAN Web App

**Goal:** Browser-based access over the local network — no internet, no client install, works on desktop and mobile. Users get a home-screen icon (PWA) and a friendly hostname; the server runs as a Windows Service on the distributor's PC.

**Decided stack:**

| Layer | Technology | Reason |
|---|---|---|
| Backend | FastAPI + Uvicorn | Minimal Python, auto OpenAPI docs, async-ready |
| Frontend | Jinja2 templates + HTMX | Server-rendered HTML, no JS framework, mobile-responsive |
| PWA | `manifest.json` + service worker | Home-screen icon on Android/iOS — no app store |
| LAN hostname | `zeroconf` → `collmgm.local` | Friendly mDNS name avoids raw IP; iOS/Android browsers support it natively |
| Database | SQLite (`sqlite3`, stdlib) | Concurrent multi-user writes; CSV cannot handle LAN concurrency |
| Windows Service | NSSM wraps Uvicorn | Auto-starts on boot, no user login required |
| Client | Chrome (Android) / Safari (iOS) | Zero install; "Add to Home Screen" once, tap icon forever |

**First-time device setup (one-off per phone):**
1. Open browser → `http://collmgm.local:8100`
2. Browser menu → Add to Home Screen
3. Tap the icon from now on

---

#### Sub-milestone 1 — SQLite migration

Replaces CSV files with SQLite. `coll_store.py` is the only layer that changes; all code above it is unaffected.

- New `coll_store_sqlite.py` implementing the same interface as `coll_store.py`
- One-time migration script: CSV → SQLite on first run, preserving the existing schema exactly
- Staging reports remain as JSON files (no change to staging layer)
- All existing tests still pass — store abstraction shields them
- Schema enhancements (new fields on users, beats, vouchers, installments) are deferred to a later milestone

**Files:** `scripts/coll_store_sqlite.py` (new), `scripts/migrate_csv_to_sqlite.py` (new), `schema.md` (updated), `generate_test_data.py` (updated)

---

#### Sub-milestone 2 — FastAPI backend + HTMX web UI

Splits `coll_workflow.py` into pure logic and adds the web layer. CLI continues to work unchanged.

- Refactor `coll_workflow.py`: strip all `input()`/`print()` into pure functions; `coll_cli.py` and the API both call the same functions
- New `scripts/coll_api.py`: FastAPI app, cookie-based session auth, endpoints mirror the CLI workflow steps
- New `templates/` directory: Jinja2 + HTMX screens for login, menu, all workflow steps, reports
- Role-based UI: each role sees only their actions (salesman → submit, supervisor → approve, distributor → post)
- PWA assets: `static/manifest.json`, `static/sw.js`, app icons
- `zeroconf` broadcasts `collmgm.local` on LAN startup

**Files:** `scripts/coll_api.py` (new), `templates/` (new), `static/` (new), `scripts/coll_workflow.py` (refactored)

---

#### Sub-milestone 3 — Windows Service packaging

Extends the existing installer to register and manage the web server as a Windows Service.

- Bundle NSSM in the installer
- Installer registers `collmgm-server` service: `nssm install collmgm-server uvicorn scripts.coll_api:app --host 0.0.0.0 --port 8100`
- Adds Windows Firewall inbound rule for port 8100 (LAN only)
- Installer upgrade-safe: service is stopped before upgrade, restarted after
- Updated Inno Setup script

**Files:** `installer/collmgm.iss` (updated), `installer/nssm.exe` (bundled)
