"""
CollMgm web server entry point.

Used by the Windows Service (NSSM) and by run_server.bat when the embedded
Python runtime is detected.  Works from any CWD — paths are derived from this
file's location.

Usage:
    python scripts\start_server.py
    python scripts\start_server.py --port 8001
"""

import argparse
import sys
from pathlib import Path

# Resolve paths relative to this file so the script works regardless of CWD.
_here = Path(__file__).resolve().parent         # scripts/
_app_root = _here.parent                        # app root ({app}\)
_packages = _app_root / "python" / "Lib" / "site-packages"

# Add embedded-Python packages dir first so bundled packages take precedence.
if _packages.exists():
    sys.path.insert(0, str(_packages))

# Add scripts/ so coll_api and its imports resolve correctly.
sys.path.insert(0, str(_here))


def _parse_args():
    p = argparse.ArgumentParser(description="CollMgm web server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8100)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    import uvicorn
    uvicorn.run("coll_api:app", host=args.host, port=args.port)
