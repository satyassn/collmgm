## Project overview

`collmgm` is a Windows CLI collection-management tool backed by CSV files. It handles the full field-collection workflow — generating voucher lists, salesman submission, supervisor approval, and distributor posting — with role-based access control.

**Current release:** `CollMgm-alpha-20260701230618`

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

**Expected output:** `Ran 69 tests in ~1.4s — OK`

### What is tested

| Test class | Coverage |
|---|---|
| `TestSanitize` | filename-safe encoding edge cases |
| `TestPasswordHashing` | PBKDF2 round-trip, wrong password, salt uniqueness |
| `TestVerifyUser` | valid login, wrong password, unknown user, system role blocked |
| `TestLoadPendingStartReports` | `stages.start == "new"` predicate; addv files ignored |
| `TestLoadPendingSubmitReports` | `stages.submit == "submitted"` predicate; all other states excluded |
| `TestInstallmentsSidecar` | round-trip, bookmark, no legacy `__status__` field |
| `TestAppendInstallmentsCSV` | dedup on `(bill_no, date)`, header creation, zero/empty skipped |
| `TestUpdateVouchersBalance` | balance arithmetic, zero detection, atomic write, lock lifecycle |
| `TestBeatLock` | exclusive acquire, double-acquire blocked, release re-enables |
| `TestFinalizeCheckpoint` | write, read, overwrite, clear |
| `TestLoadVouchersRaw` | reads rows, missing file raises |
| `TestArchiveCompleted` | vouchers and installments moved to completed files |

Each test class uses an isolated temp directory; `coll_store`'s path constants are patched per-test so no real data files are touched.

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
| `schema.md` | Canonical CSV schemas and validation rules |
| `pipeline.md` | Collection workflow state reference |
| `CLAUDE.md` | Architecture, module contracts, and development principles |
| `scripts/coll_store.py` | All path constants and I/O primitives |
| `scripts/coll_data.py` | Data loading and query functions |
