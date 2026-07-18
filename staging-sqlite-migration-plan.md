# Migrate staging state from JSON files to SQLite (schema v2)

## Context

Master data already lives in SQLite (`data/collmgm.db`), but all in-flight workflow state is still files: collection reports (`staging/coll*.json` + `-installments.json` sidecar + `.txt`), add-voucher batches (`staging/addv*.json`), file-based beat locks / post-claim locks / a finalize checkpoint, and a write-only `archive/` directory. The upcoming LAN web app needs concurrent-safe staging; two browser sessions can currently clobber the same JSON file. User confirmed scope: **migrate both pipelines (coll + addv), move archive into DB tables, drop the staging `.txt` sidecar** (printable output stays via the existing `prints/` pipeline).

Full design detail (function-by-function mapping, complete DDL): `C:\Users\s3teq\.claude\plans\wondrous-cooking-otter-agent-a83129b9fca4d3977.md`.

## Key design decisions

1. **Report identity stays the stem string** (`coll{YYYYMMDD}-beat_salesman-{beat}_{salesman}`, `addv{ts}-{user}`), stored as a UNIQUE `report_key`/`batch_key`. coll_api URLs (`/coll/post/{stem}`), `_STEM_RE`, form fields, and all templates need zero changes; in-code handles change type Path → str only.
2. **The report row IS the beat lock**: `UNIQUE(beat)` on `coll_reports`; `create_coll_report` INSERT failing with `IntegrityError` → lock conflict. Deletes `acquire/release_beat_lock`; orphaned lock files become impossible.
3. **Post becomes one transaction; post-claim lock and finalize checkpoint are deleted.** Verified against `coll_orchestrate.py:420-491`: everything after `apply_post_to_db`'s commit (staging JSON save, archive rename, checkpoint, lock release) moves into the DB, so `post_coll_report(report_key, ...)` runs `BEGIN IMMEDIATE` → guarded `DELETE FROM coll_reports WHERE id=? AND stage_submit='confirmed'` (rowcount 0 → stale/already-posted, replaces the claim) → existing master-table write helpers on the same connection → insert into `archived_reports` → COMMIT. A crash rolls back everything; `PostOutcome` drops `step_failed`/`archive_warning`.
4. **Archive = one lenient payload table** `archived_reports(report_key indexed non-unique, kind 'coll'|'addv', archived_at, payload TEXT)` — archive is verified write-only (nothing reads `ARCHIVE_DIR`), so normalized archive tables would be dead weight; this mirrors today's rename semantics and absorbs every legacy shape.
5. **Installments sidecar collapses** into `payment`/`payment_date` columns on `coll_report_vouchers` + a `bookmark` column on the header row (`_load_installments(key)` keeps its return shape `({bill_no: {payment, date}}, bookmark)`).
6. **Store API stays signature-compatible where cheap**: scanners keep names and return `[(report_key, dict)]` with the exact current dict shape (including reconstructed `stages`), so coll_orchestrate/coll_workflow/coll_api diffs stay small. All DB access derives the connection at call time (like `_db_path()`), so tests patch only `coll_store.DATA_DIR`.

## New tables (schema v2, `PRAGMA user_version` 1 → 2)

- `coll_reports(id PK, report_key UNIQUE, selection_type IN ('beat','beat_salesman'), beat UNIQUE, salesman, date, stage_start IN ('new','confirmed'), stage_submit IN ('','inprogress','submitted','returned','confirmed'), bookmark, created_at)` — no `stage_post`: posting deletes the row and archives in the same tx.
- `coll_report_vouchers(report_id FK CASCADE, bill_no, voucher_date, date, balance, payment, payment_date, beat, salesman, PK(report_id, bill_no))`.
- `addv_batches(id PK, batch_key UNIQUE, mode, created_by, created_at, stage_confirm IN ('','confirmed'))`.
- `addv_batch_vouchers(batch_id FK CASCADE, bill_no UNIQUE table-wide — this IS the staged-duplicate guard, date, amount, balance, beat, salesman, created_by, created_at)`.
- `addv_batch_installments(id PK, batch_id FK CASCADE, bill_no, date, amount, salesman, created_by, created_at)`.
- `archived_reports` as above.

Money columns stay TEXT with the existing `NOT GLOB '*[^0-9.]*'` CHECK style (empty allowed only for `payment`).

## Files to modify

| File | Change |
|---|---|
| [scripts/coll_store.py](scripts/coll_store.py) | New DDL in `_TABLE_DDL` + `_migrate_schema_v2` + `_migrate_staging_files_to_db`. Rewrite staging functions as SQL (`_load_pending_start_reports`, `_load_pending_submit_reports`, `load_addv_*` keep names). New: `create_coll_report` (INSERT-as-lock), `save_coll_report`/`save_addv_batch` (upsert, delete+reinsert children), `load_coll_report`, `post_coll_report`, `post_addv_batch`. `cancel_staging_report(report_key)` drops the beat arg. `write_collection_text` → `build_collection_text(...) -> str`. Delete: beat/post locks, checkpoint fns, `archive_files`, `_installments_path`, `save/load_collection_json`, `list_staging_reports`, `ensure_staging_dir`. |
| [scripts/coll_data.py](scripts/coll_data.py) | Six raw `STAGING_DIR` globbers (`_find_any_active_beat_report`, `load_active_beat_statuses`, `_load_confirmed_start_reports`, `_load_submit_confirmed_reports`, `load_addv_pending_confirm_by_beat`) become thin wrappers over new store query helpers. `_find_confirmed_start_report` is unused — delete. |
| [scripts/coll_orchestrate.py](scripts/coll_orchestrate.py) | `GenerateOutcome.json_path/txt_path` → `report_key`; `generate_collection_list` uses `create_coll_report`; post path shrinks to load → stage check → `validate_staged_report` → `post_coll_report`; drop checkpoint/claim/txt regeneration. |
| [scripts/coll_workflow.py](scripts/coll_workflow.py) | Path handles → keys; addv flow uses `save_addv_batch`/`post_addv_batch` (replaces the non-atomic 4-step post at :1206-1224); display printable list via `build_collection_text` string. |
| [scripts/coll_api.py](scripts/coll_api.py) | `_load_staging_report(stem)` keeps name/regex, body becomes DB lookup; `p.stem` → key in 4 listings; remove stale-checkpoint banner from `/coll/post` route + `templates/coll/post.html`. |
| [scripts/coll_cli.py](scripts/coll_cli.py) | `display_report_for_review` takes text string instead of txt_path (data-driven fallback already exists ~:690). |
| tests | See below. |
| CLAUDE.md, schema.md, pipeline.md | Document schema v2, removed staging dir, migration behavior. |

## One-time file import (`_migrate_staging_files_to_db`)

Runs inside `init_db()` when `user_version < 2`, in one transaction with the DDL:
1. **Refuse with `MigrationError`** if `staging/.finalize_checkpoint.json` exists (interrupted post = ambiguous state; operator must resolve first).
2. Pre-scan staging files Python-side (à la `_scan_v1_violations`); refuse with file/row listing if active data violates v2 CHECKs.
3. Import `coll*.json` (+ installments sidecars: scalar legacy entries → `{payment, date: ''}`, `__bookmark__` → column) and `addv*.json`; already-posted files go straight to `archived_reports`.
4. Import `archive/*.json` best-effort as raw payloads (key = stem incl. `_dupN`; unparseable files skipped, never refused).
5. Stamp `user_version = 2`; after commit, move consumed files to `archive/pre_v2_backup/` so `staging/` ends empty and the DB is unambiguously authoritative (failed move is non-fatal — version stamp prevents re-import).

## Implementation order (each step ends green)

1. **Store layer**: DDL + CRUD/query functions + unit tests, old file functions untouched. Verify: `python -m unittest discover -s tests -v`.
2. **Migration functions** implemented and direct-tested, NOT yet wired into `init_db` (wiring early would strand the still-file-based app).
3. **The flip** (one commit — the Path→str handle crosses every module via `.stem`/`.with_suffix`/`read_text` and can't be split): coll_data/orchestrate/workflow/api/cli switch to keys, wire migration into `init_db`, template edit, update existing tests.
4. **Dead-code deletion** (locks, checkpoint, archive_files, sidecar helpers) with grep verification; extend `reset_test_data_tables` in generate_test_data.py to the 5 new tables.
5. **Docs + end-to-end**: update CLAUDE.md/schema.md/pipeline.md; manual run on a copy of real data.

## Test plan

- Existing tests drop `STAGING_DIR`/`ARCHIVE_DIR` patches (kept only in migration tests); seed via new save functions; patch only `coll_store.DATA_DIR`.
- New: migration legacy-shape matrix (selection_type `beat`, missing `date`, `finalize` vs `post` key, scalar installments, `_dupN` stems, checkpoint-present refusal); beat-lock semantics via `create_coll_report`; atomic post (injected failure leaves staging + master untouched; two concurrent posts → exactly one succeeds); addv UNIQUE duplicate guard incl. resume re-save (delete+reinsert children so resume doesn't trip `UNIQUE(bill_no)`).

## Verification

1. `python -m unittest discover -s tests -v` — all green.
2. Copy real `data/` + `staging/` + `archive/` to a scratch dir; run `run.bat` once → verify migration imports all files, `staging/` empties into `archive/pre_v2_backup/`, active reports appear in the CLI exactly as before.
3. Walk the full 5-stage workflow end-to-end in the CLI (start → approve → submit → approve → post) and confirm master tables + `archived_reports` row.
4. Same walk through the web UI (`run_server.bat`), confirming stem-based URLs still resolve; attempt two simultaneous posts of one report → one succeeds, one gets the stale-report message.
5. Walk the addv flow: add batch → confirm → post; verify duplicate bill_no rejected at add.

## Risks

- Migration refuses while a finalize checkpoint exists — intentional; document the operator step.
- An un-upgraded file-based client pointed at an upgraded DB sees empty staging — ship CLI and web together; note in release notes.
- Concurrent web posts may hit SQLite's 5s busy timeout → consider raising `busy_timeout` in `get_db()`.
