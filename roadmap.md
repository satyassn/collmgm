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

### REST API layer (for GUI / advanced CLI)

**Goal:** Expose the collection workflow as a REST API so a web GUI, mobile client, or a richer CLI (e.g. Textual/Rich TUI) can be built on top without re-implementing business logic.

**Design sketch:**
- Add a thin HTTP layer (FastAPI recommended — minimal boilerplate, auto OpenAPI docs) over the existing `coll_data` / `coll_store` modules
- The current architecture is already well-suited: `coll_data` and `coll_store` have no I/O side-effects and can be called directly from API handlers
- `coll_workflow.py` will need to be split into pure-logic functions (no `input()`/`print()`) that both the CLI and the API can call
- Suggested endpoints:
  - `GET  /beats` — list beats with pending summary
  - `GET  /reports` — list active staging reports and their stage/status
  - `POST /collections/start` — create and confirm a start report for a beat
  - `POST /collections/submit` — submit payment entries for a report
  - `POST /collections/post` — post and archive a submitted report
  - `GET  /reports/{id}/vouchers` — voucher list for a report
- Authentication: session token or API key (reuses RBAC role definitions from alpha)
- Stateless: staging files on disk remain the source of truth; API is a façade

**Dependency:** RBAC is complete (alpha) — the API auth model should reuse the same role definitions.

**Files affected:** new `api/` package or `scripts/coll_api.py`; `coll_workflow.py` refactored to separate pure logic from CLI I/O

---

### Schema Enhancement

**Goal:** Enrich the data model with fields needed for production use and decide whether CSV files remain the storage backend or are replaced by a database.

**Fields to add (candidate list — confirm before implementation):**

| File | Candidate fields | Reason |
|------|-----------------|--------|
| `users.csv` | `beat` (assigned beat for salesmen), `active` | Beat ownership enforcement, soft-delete |
| `beats.csv` | `cp` (collection point / zone), `salesman` (default assigned salesman) | Multi-CP support, beat→salesman mapping |
| `vouchers.csv` | `customer_name`, `customer_id`, `party_code`, `due_date` | Customer traceability, overdue tracking |
| `installments.csv` | `collection_date` (separate from voucher date), `collected_by`, `verified_by` | Audit trail, who collected vs who verified |

**Storage decision — CSV vs Database:**

| | CSV (keep) | SQLite / PostgreSQL |
|--|-----------|---------------------|
| Pros | Zero dependencies, human-readable, git-diffable, simple backup | Referential integrity, concurrent access, query flexibility, indexing |
| Cons | No transactions, no foreign keys, full-file rewrites on update, fragile under concurrency | Adds a runtime dependency, harder to inspect raw data |
| Best for | Single-user CLI POC, offline-first, small data | Multi-user, REST API, scale |

**Recommendation:** Migrate to **SQLite** when the REST API milestone begins — it is file-based (no server), already ships with Python (`sqlite3`), supports transactions (critical for the post batch update), and the `coll_store.py` I/O layer provides a clean seam to swap backends without touching workflow or UI code. CSV can remain as an import/export format.

**Files affected:** `coll_store.py` (backend swap), `schema.md` (updated field specs), `generate_test_data.py` (updated seed logic)
