# How to Build the CollMgm Windows Installer

## Prerequisites

- Windows machine (or Linux with Wine)
- [Inno Setup 6.x](https://jrsoftware.org/isinfo.php)
- Python + pip on PATH (system Python, for `setup_build_env.bat`)
- Internet access to download Python embeddable package and NSSM (one-time per new version)

---

## Step 1 — Refresh Test Data (optional, on Linux/Mac)

```bash
python3 scripts/generate_test_data.py --seed 42
```

---

## Step 2 — Download Python Embeddable Runtime

1. Go to https://www.python.org/downloads/windows/
2. Download **Python 3.13.x — Windows embeddable package (64-bit)**
   (filename: `python-3.13.x-embed-amd64.zip`, ~12 MB)
3. Extract the zip contents into `packaging/python/`

After extraction, `packaging/python/` should contain:
```
python.exe
python313.dll
python313.zip
python313._pth
...
```

---

## Step 3 — Download NSSM

NSSM (Non-Sucking Service Manager) runs the web server as a Windows Service.

1. Go to https://nssm.cc/download
2. Download **nssm 2.24 (2014-08-31)** zip
3. Extract `nssm-2.24\win64\nssm.exe` into `packaging/nssm/nssm.exe`

> `packaging/nssm/` is in `.gitignore` — do not commit the binary.

---

## Step 4 — Prepare the Build Environment

Run this from the `packaging/` directory (or project root):

```bat
packaging\setup_build_env.bat
```

This:
- Installs `fastapi`, `uvicorn`, `jinja2`, `python-multipart` into
  `packaging/python/Lib/site-packages/`
- Verifies the embedded Python can import them
- Warns if NSSM is missing

Re-run whenever `requirements.txt` changes.

---

## Step 5 — Build the Installer

```bat
make build
```

| Command | Output EXE |
|---|---|
| `make build` | `CollMgm-alpha-20260701143022-Setup.exe` |
| `make build RELEASE=beta` | `CollMgm-beta-20260701143022-Setup.exe` |

---

## Step 5b — Deploy (install + start service from the latest build)

```bat
make deploy                 # installs the newest CollMgm-alpha-*-Setup.exe
make deploy RELEASE=beta    # installs the newest beta build
```

Runs the most recent installer for that RELEASE silently. This registers and starts
the `collmgm-server` Windows Service and opens firewall port 8100. `make deploy` does
**not** build — run `make build` first.

Run from an **elevated** shell for a clean synchronous install; from a normal shell,
accept the UAC prompt. Install log: `packaging/dist/deploy.log`.

---

## Installer Behaviour

### What the installer does

1. Copies the embedded Python runtime, application scripts, web templates, and static assets.
2. Copies `tools\nssm.exe` (NSSM).
3. Creates `data\`, `staging\`, `archive\`, `prints\`, `logs\`, and `config\` directories
   (never overwritten on upgrade).
4. Runs `service_setup.bat` as administrator to:
   - Register `collmgm-server` as a Windows Service (auto-start).
   - Configure NSSM log rotation to `logs\server.log`.
   - Add a Windows Firewall inbound rule for TCP port 8100 (LAN profiles only).
5. Starts the service immediately.

### Upgrade behaviour

Files that **always** overwrite:
- `scripts/`, `python/`, `templates/`, `static/`, `*.bat`

Files that are **created once** and never overwritten:
- `data/`, `staging/`, `archive/`, `prints/`, `logs/`, `config/`

Before overwriting files, the installer stops and de-registers the old service so
running processes do not hold file locks.  The service is reinstalled and restarted
after the files are in place.

### Uninstall

The uninstaller:
- Stops and removes the `collmgm-server` service.
- Removes the `CollMgm Web Server` firewall rule.
- Removes all installed files **except** `data\`, `staging\`, `archive\`, `prints\`, `logs\`, `config\`
  (customer data is never deleted).

---

## Step 6 — Verify the Installer

Test on a clean Windows machine (or VM) that does **not** have Python installed:

1. Run `CollMgm-alpha-<stamp>-Setup.exe` as administrator.
2. Complete the wizard.
3. When prompted, open `http://localhost:8100` in a browser.
4. Log in with distributor credentials and verify all workflow screens load.
5. From another PC on the same LAN, open `http://<server-name>:8100`.
6. Verify the Start-up Service is registered:
   ```bat
   sc query collmgm-server
   ```
7. Reboot the server PC and confirm the service auto-starts.

---

## Step 7 — Deliver to Customer

Send the `CollMgm-alpha-<stamp>-Setup.exe` (~20–30 MB) and `BETA_GUIDE.txt`.

---

## Rebuilding an Old Release

```bat
git checkout build/alpha-20260701230618
make build RELEASE=alpha STAMP=20260701230618
```

---

## Developer Targets

```bat
make run           # Launch CLI (system Python)
make deploy        # Install & start the newest built installer as a service (needs admin)
make install-service   # Register service using system Python (dev testing)
make uninstall-service # Remove dev service
```

---

## To Update for the Next Release

1. Update code in `scripts/`.
2. Re-run `setup_build_env.bat` if `requirements.txt` changed.
3. Bump `#define MyAppVersion` in `packaging/setup.iss` for a new version number.
4. Run `make build RELEASE=<label>`.
