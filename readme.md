## Project overview

`collmgm` is a Windows collection-management tool — CLI and a LAN-hosted mobile-responsive web app, both backed by the same SQLite-backed store. It handles the full field-collection workflow — generating voucher lists, salesman submission, supervisor approval, and distributor posting — with role-based access control.

The web app additionally supports a physical-voucher verification workflow and a correction-request system on both supervisor approval screens (see `roadmap.md`).

**Current release:** `CollMgm-alpha-20260701230618` (see `roadmap.md` for everything shipped since, on the ongoing alpha build line)

---

## Running the app

```
run.bat
```

Launches `scripts/collmenu.py`. Login with your username and password when prompted.

---

## Running tests

Tests use the Python standard library only — no extra packages required.

```
python -m unittest discover -s tests -v
```

Run from the **project root** (not from `scripts/` or `tests/`).

**Expected output:** `Ran 321 tests in ~80s — OK`

### What is tested

Three files, each targeting one architectural layer:

| File | Layer | Coverage |
|---|---|---|
| `tests/test_coll_store.py` | Persistence (`coll_store.py`) | CSV/SQLite reads-writes, schema migrations, permission backfills, the `corrections` table and its atomic apply, locks and checkpoints |
| `tests/test_coll_orchestrate.py` | Shared workflow logic (`coll_orchestrate.py`) | Stage transitions for all 5 collection-workflow steps, payment/balance validation, the correction-request lifecycle (apply/reject/withdraw, auto-settle on resubmission), physical-voucher verification gates |
| `tests/test_coll_api.py` | Web app (`coll_api.py`) | Full route + session + permission stack against a live server (RBAC/IDOR checks, stage guards, tamper defenses, the verification and correction-request screens end to end) |

A few notable test classes from `test_coll_store.py` (the original CLI-era coverage):

| Test class | Coverage |
|---|---|
| `TestSanitize` | filename-safe encoding edge cases |
| `TestPasswordHashing` | PBKDF2 round-trip, wrong password, salt uniqueness |
| `TestVerifyUser` | valid login, wrong password, unknown user, system role blocked |
| `TestInstallmentsSidecar` | round-trip, bookmark, no legacy `__status__` field |
| `TestUpdateVouchersBalance` | balance arithmetic, zero detection, atomic write, lock lifecycle |
| `TestBeatLock` | exclusive acquire, double-acquire blocked, release re-enables |
| `TestArchiveCompleted` | vouchers and installments moved to completed files |

...and from the web/orchestrate suites added for iteration3:

| Test class | Coverage |
|---|---|
| `TestStartVerification` / `TestSubmitVerification` (API) | Verification checkbox persistence, all guard status codes, the approve hard-gate, key popped on approve |
| `TestCorrections` (API) | Raise/withdraw/queue/apply/reject for the four master-data kinds, role guards, derived active/history visibility |
| `TestCollectionCorrections` (API) | Collection-amount-only raise, supervisor-or-distributor resolution, Return + auto-settle on matching resubmission |

Each test class uses an isolated temp directory (or a live server on a random port, for the API suite); path constants are patched per-test so no real data files are touched.

---

## Branching strategy

Three long-lived branches, each with a distinct purpose:

| Branch | Purpose |
|---|---|
| `main` | Roadmap development — next major milestones (REST API, SQLite) |
| `alpha/dev` | Alpha-line enhancements — new features targeting the next alpha release |
| `alpha/release` | Customer hotfixes — patches to the shipped alpha build only |

### Rules

- **No direct commits** to any of the three branches. All changes go through a pull request.
- Branch protection is enforced on GitHub (Settings → Branches).

### Typical flows

**Hotfix for a customer issue:**
```
git checkout alpha/release
git checkout -b fix/your-fix-description
# ... make changes ...
git push origin fix/your-fix-description
# open PR → alpha/release
# cherry-pick the fix into alpha/dev and main if applicable
```

**Alpha-line enhancement:**
```
git checkout alpha/dev
git checkout -b feat/your-feature-description
# ... make changes ...
git push origin feat/your-feature-description
# open PR → alpha/dev
```

**Roadmap feature:**
```
git checkout main
git checkout -b feat/your-feature-description
# ... make changes ...
git push origin feat/your-feature-description
# open PR → main
```

### Releases

Releases are tagged on `alpha/release` (hotfixes) or `alpha/dev` (new alpha drops) using the timestamp format `CollMgm-alpha-YYYYMMDDHHMMSS`. The `main` branch is tagged separately when a new major milestone ships.

---

## Key files

| File | Purpose |
|---|---|
| `roadmap.md` | Release history and planned milestones |
| `schema.md` | Canonical CSV/SQLite schemas and validation rules |
| `pipeline.md` | Collection workflow state reference, incl. the verification/correction gates |
| `CLAUDE.md` | Architecture, module contracts, and development principles |
| `scripts/coll_store.py` | All path constants and I/O primitives |
| `scripts/coll_data.py` | Data loading and query functions |
