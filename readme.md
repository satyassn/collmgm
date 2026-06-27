## Project overview
`collmgm` is a proof-of-concept CLI collection-management tool. It uses CSV files as the primary datastore and is being developed iteratively.

### Current iteration
- Iteration 1 is limited to schema confirmation and test-data generation only.
- No collection workflow implementation, no staging/merge CLI, no audit or backup implementation in iteration 1.
- The generator writes directly into `data/*.csv` using a dedicated test data user.

## Important files
- `schmema.md` — main design and schema specification.
- `Readme.md` — high-level project description and functional goals.
- `ITERATION1_PLAN.md` — iteration 1 scope and generator specification.

## Key schema notes
- `users.csv`: `name,role`
  - initial users include `dist,distributor`, `supervisor,supervisor`, and `saleman1..saleman5`.
- `beats.csv`: `name`
  - sample beats are `beat1` through `beat10`.
- `vouchers.csv`: `bill_no,date,amount,balance,beat,salesman,created_by,created_at`
- `installments.csv`: `bill_no,date,amount,salesman,created_at`

## Running Tests

Tests use the Python standard library only — no extra packages required.

```
python -m unittest discover -s tests -v
```

Run from the **project root** (not from `scripts/` or `tests/`).

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

**Expected output:** `Ran 69 tests in ~1.4s — OK`

---

## Development guidance
- Use `PLAN.md` as the authoritative design source.
- For iteration 1, implement only a test data generator and sample CSV files under `data/`.
- Keep dependencies minimal; prefer standard Python and built-in CSV handling.
- Do not implement merge, staging, audit, or user/beat management until iteration 2.
- Keep the project structure simple: scripts under `scripts/`, or a small package under `collmgm/`.
