---
name: verify
description: How to run and drive collmgm (web + CLI) against an isolated sandbox for end-to-end verification without touching the real data/ dir or dev DB.
---

# Verifying collmgm changes

Never drive the app against the real `data/` dir — logins are unknown (hashed) and runs mutate the DB/staging. Boot against a sandbox instead.

## Sandbox boot (web)

Path constants (`DATA_DIR`, `STAGING_DIR`, `ARCHIVE_DIR`, `PRINTS_DIR`) are imported **by value** into each module — patch them on every module that has them (`coll_store`, `coll_data`, `coll_orchestrate`, `coll_api`, `coll_workflow`), same as `tests/test_coll_api.py` does.

Boot script skeleton (run from a scratch dir):

```python
import sys; from pathlib import Path
sys.path.insert(0, r"<repo>\scripts")
# mkdir sandbox/{data,staging,archive,prints}; patch constants on each module
import coll_store, coll_data, coll_orchestrate, coll_api
# ... setattr(mod, "DATA_DIR", SANDBOX/"data") etc ...
coll_store.ensure_db()
# seed: INSERT users (coll_store.hash_password("pw")), beats,
#       permissions from repo data/permissions.csv (INSERT OR IGNORE),
#       staging coll*.json files written directly (see tests/_write_staging_report)
import uvicorn; uvicorn.run(coll_api.app, host="127.0.0.1", port=8123, log_level="warning")
```

Drive with curl + cookie jar: `curl -c s.jar -d "username=sup&password=pw" /login` (303 = success), then `-b s.jar` for the rest. Permission-denied pages contain "have permission for this action"; form errors render as `<p class="alert alert-error">`.

## Sandbox boot (CLI)

Same patching, then `import collmenu; collmenu.main()` with prompts piped via stdin (`printf "sup\npw\n3\n..." | python boot_cli.py`). Login uses plain input (not getpass), so piping works. **Stub `os.startfile` before importing coll_workflow** or print/preview flows will pop real browser windows.

## Gotchas

- Menu numbering is role-dependent (built from permissions × `ACTION_REGISTRY` order); capture output once to learn the numbers before scripting a sequence.
- To test `init_db` migrations against real data, copy `data/collmgm.db` to a scratch dir and point `coll_store.DATA_DIR` at the copy.
- Test suite: `python -m unittest discover -s tests` (~35s, includes live-uvicorn API tests).
