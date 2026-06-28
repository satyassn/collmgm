## What agents should do
- Iterative mode of development.
- Follow project documentation; read `CLAUDE.md` first for project context and architecture.
- Every iteration has a plan file (e.g. `iteration2.md`). Stick to plan scope; warn before any deviation.
- Keep dependencies minimal; prefer standard Python and built-in CSV handling (no third-party packages in core scripts).
- Respect the module layering contract defined in `CLAUDE.md` — coll_store has no upstream deps; coll_data imports only from coll_store; coll_workflow orchestrates via coll_cli + coll_data + coll_store.

## What agents should not do
- Do not use "Confirm" for supervisor gatekeeping actions — use "Approve". Do not use "Finalize" for distributor write-to-master actions — use "Post". See `CLAUDE.md` UI naming conventions.
- Do not use "collection report" for the working document flowing through the pipeline — it is a "collection list". "Collection report" is reserved for the Reports sub-menu analytical views.
- Do not change the agreed CSV schemas without explicit user approval.
- Do not add print()/input() calls inside coll_store.py or coll_data.py.
- Do not put file path constants in coll_workflow.py — all paths live in coll_store.py.
- Do not use float for monetary values — always use Decimal.
- Do not add backup, audit logging, or RBAC until those milestones are explicitly started.
