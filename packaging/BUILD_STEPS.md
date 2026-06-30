# How to Build the CollMgm Beta Windows Installer

## Prerequisites

- A Windows machine (or Linux with Wine) to run Inno Setup
- [Inno Setup 6.x](https://jrsoftware.org/isinfo.php) — free, download from official site
- Internet access to download Python embeddable package (one-time)

---

## Step 1 — Refresh Test Data (on Linux/Mac)

Run this from the project root to regenerate reproducible test data:

```bash
python3 scripts/generate_test_data.py --seed 42
```

This overwrites `data/vouchers.csv` and `data/installments.csv` with a
consistent dataset (1601 vouchers, 5603 installments, date range 2020-01-06).

---

## Step 2 — Download Python Embeddable Runtime

1. Go to https://www.python.org/downloads/windows/
2. Download **Python 3.12.x — Windows embeddable package (64-bit)**
   (filename looks like `python-3.12.x-embed-amd64.zip`, ~12 MB)
3. Extract the zip contents into `packaging/python/`

After extraction, `packaging/python/` should contain files like:
```
packaging/python/
  python.exe
  python312.dll
  python312.zip
  ...
```

---

## Step 3 — Build the Installer

The build number is stored in `packaging/build_number.txt` and auto-increments on every build.
The release label defaults to `alpha`; override with `RELEASE=beta` etc.

| Command | Output EXE |
|---|---|
| `make build` | `CollMgm-alpha-Build1-Setup.exe` |
| `make build` (again) | `CollMgm-alpha-Build2-Setup.exe` |
| `make build RELEASE=beta` | `CollMgm-beta-Build3-Setup.exe` |

### Option A: On Windows (recommended)

```bat
make build
rem or
make build RELEASE=beta
```

Requires: GNU Make (e.g. via Git for Windows) and Inno Setup 6.

### Option B: Inno Setup IDE (no build counter increment)

1. Install Inno Setup 6 from https://jrsoftware.org/isinfo.php
2. Open `packaging/setup.iss` in the Inno Setup Compiler
3. Press **F9** (or Build > Compile)
4. Output: `packaging/dist/CollMgm-alpha-Build0-Setup.exe` (uses `#ifndef` defaults)

### Option C: On Linux using Wine

```bash
# Install Wine and Inno Setup under Wine (one-time)
sudo apt install wine
wine InnoSetup-6.x.exe   # run the Inno Setup installer under Wine

# Then build via make
make build RELEASE=alpha
```

Output will be at `packaging/dist/CollMgm-alpha-Build<N>-Setup.exe`.

---

## Step 4 — Verify the Installer

Test on a clean Windows machine or VM that does **not** have Python installed:

1. Run `CollMgm-Beta-Setup.exe`
2. Complete the wizard (Next → Next → Finish)
3. Double-click the desktop shortcut
4. Verify the menu appears and all 3 workflow steps work end-to-end

---

## Step 5 — Deliver to Customer

Send `CollMgm-Beta-Setup.exe` (~15–20 MB) via email or a file-sharing link
(Google Drive, OneDrive, WeTransfer, etc.).

Also attach `BETA_GUIDE.txt` separately so they can read it before installing.

---

## To Update for the Next Release

1. Update code in `scripts/`
2. Re-run test data generation if schema changed
3. Bump `#define MyAppVersion` in `packaging/setup.iss` for a new version number
4. Rebuild installer — `make build RELEASE=<label>` (build number increments automatically)
