@echo off
rem CollMgm - prepare the embedded Python runtime for a web-enabled build.
rem Run this once (or after updating requirements.txt) before running make build.
rem Must be run from the project root or the packaging\ directory.
rem Requires: Python + pip on PATH (any version), internet access for first run.

setlocal
cd /d "%~dp0"

rem ---- Locate the embedded Python ----------------------------------------
set "EMB_PY=%~dp0python\python.exe"
if not exist "%EMB_PY%" (
  echo ERROR: Embedded Python not found at %EMB_PY%
  echo Download the Python 3.x Windows embeddable package and extract it to
  echo   packaging\python\
  echo See BUILD_STEPS.md for details.
  exit /b 1
)

rem ---- Detect embedded Python version (for cross-version pip install) ------
for /f "tokens=2 delims= " %%v in ('"%EMB_PY%" --version 2^>^&1') do set "EMB_VER=%%v"
for /f "tokens=1,2 delims=." %%a in ("%EMB_VER%") do (
  set "PY_MAJ=%%a"
  set "PY_MIN=%%b"
)
set "PY_VER=%PY_MAJ%.%PY_MIN%"
echo Embedded Python: %EMB_VER%  (will download cp%PY_MAJ%%PY_MIN%-win_amd64 wheels)

rem ---- Locate NSSM -----------------------------------------------------------
set "NSSM_EXE=%~dp0nssm\nssm.exe"
if not exist "%NSSM_EXE%" (
  echo.
  echo WARNING: NSSM not found at %NSSM_EXE%
  echo Download nssm-2.24.zip from https://nssm.cc/download
  echo Extract nssm-2.24\win64\nssm.exe to packaging\nssm\nssm.exe
  echo.
)

rem ---- Install web packages for the correct Python version -----------------
rem Use --python-version and --platform so pip downloads wheels matching the
rem embedded runtime (cp313-win_amd64) even when the system pip is a different
rem Python version (e.g. cp314).  --only-binary :all: prevents source builds.
rem
rem We use plain "uvicorn" (not uvicorn[standard]) to avoid pulling in native
rem extensions (httptools, watchfiles) that may not have pre-built wheels for
rem every minor Python version.  Performance is identical for a LAN server.

set "TARGET=%~dp0python\Lib\site-packages"
echo Installing web packages into %TARGET% ...

python -m pip install ^
  fastapi ^
  uvicorn ^
  jinja2 ^
  python-multipart ^
  --target "%TARGET%" ^
  --python-version %PY_VER% ^
  --platform win_amd64 ^
  --implementation cp ^
  --only-binary :all: ^
  --quiet

if %ERRORLEVEL% neq 0 (
  echo ERROR: pip install failed.  Ensure Python and pip are on PATH.
  exit /b 1
)
echo Packages installed.

rem ---- Patch ._pth to include Lib\site-packages ---------------------------
rem This lets the embedded Python find packages without manual sys.path edits.
for %%f in ("%~dp0python\python*._pth") do set "PTH_FILE=%%f"
findstr /c:"Lib\site-packages" "%PTH_FILE%" >nul 2>&1
if %ERRORLEVEL% neq 0 (
  echo Lib\site-packages>> "%PTH_FILE%"
  echo Patched %PTH_FILE% to include Lib\site-packages
)

rem ---- Verify: embedded Python can import the key packages -----------------
echo Verifying imports with embedded Python...
"%EMB_PY%" -c "import fastapi, uvicorn, jinja2; print('  OK: fastapi', fastapi.__version__, '/ uvicorn', uvicorn.__version__)"
if %ERRORLEVEL% neq 0 (
  echo.
  echo ERROR: Import verification failed.
  echo Check %TARGET% -- packages may be missing or wrong Python version.
  exit /b 1
)

echo.
echo Build environment ready.  Run  make build  to create the installer.
endlocal
