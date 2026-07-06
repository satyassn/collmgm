# Path to Inno Setup compiler — override if installed elsewhere
ISCC       ?= C:/Users/s3teq/AppData/Local/Programs/Inno Setup 6/ISCC.exe
# Release label passed to installer (alpha, beta, ...)
RELEASE    ?= alpha

ISS        := packaging/setup.iss
DIST       := packaging/dist

.DEFAULT_GOAL := help

.PHONY: help build deploy run serve install-service uninstall-service clean

help:
	@echo "Usage: make [target] [RELEASE=alpha] [STAMP=yyyymmddhhmmss]"
	@echo
	@echo "  build              Build the Windows installer EXE"
	@echo "                     Requires: Inno Setup 6, packaging/python/, packaging/nssm/"
	@echo "                     Run packaging/setup_build_env.bat before first build"
	@echo "  deploy             Install & start the newest built installer as a service (needs admin)"
	@echo "  run                Launch the CLI (system Python)"
	@echo "  serve              Start the web server (system Python, port 8100)"
	@echo "  install-service    Register collmgm-server Windows Service (dev, needs admin)"
	@echo "  uninstall-service  Remove collmgm-server Windows Service (dev, needs admin)"
	@echo "  clean              Delete $(DIST)/"
	@echo
	@echo "To rebuild an old release:"
	@echo "  git checkout build/<release>-<stamp>"
	@echo "  make build RELEASE=<release> STAMP=<stamp>"

build:
	@set -e; \
	BUILD=$$([ -n "$(STAMP)" ] && echo "$(STAMP)" || date +%Y%m%d%H%M%S); \
	echo Building $(RELEASE) $$BUILD...; \
	MSYS_NO_PATHCONV=1 "$(ISCC)" /DMyAppRelease=$(RELEASE) /DMyAppBuild=$$BUILD "$(ISS)"; \
	git tag "build/$(RELEASE)-$$BUILD" 2>/dev/null \
	  && echo "Tagged: build/$(RELEASE)-$$BUILD" \
	  || echo "Note: tag build/$(RELEASE)-$$BUILD already exists, skipping"

# Install and start the newest built installer as the collmgm-server service.
# Does NOT build — run  make build  first.  Requires admin (UAC will prompt).
deploy:
	@set -e; \
	EXE=$$(ls -t "$(DIST)"/CollMgm-$(RELEASE)-*-Setup.exe 2>/dev/null | head -1); \
	if [ -z "$$EXE" ]; then \
	  echo "ERROR: no installer for RELEASE=$(RELEASE) in $(DIST)/. Run 'make build' first."; \
	  exit 1; \
	fi; \
	echo "Deploying $$EXE ..."; \
	echo "(A UAC prompt is expected — admin rights are required to register the service.)"; \
	MSYS_NO_PATHCONV=1 "$$EXE" /SILENT /SUPPRESSMSGBOXES /NORESTART /LOG="$(DIST)/deploy.log"; \
	echo "Installed. Service status:"; \
	sc query collmgm-server || true; \
	echo "CollMgm is running at http://localhost:8100 (LAN: http://$$COMPUTERNAME:8100)"

run:
	python -c "import sys; sys.path.insert(0, 'scripts'); import collmenu; collmenu.main()"

serve:
	python scripts/start_server.py

# Developer service targets — requires an elevated (admin) shell on Windows.
# These register the service against the *system* Python so run from an
# admin prompt after  pip install -r requirements.txt .
NSSM_DEV   ?= nssm
SVC_PYTHON ?= $(shell python -c "import sys; print(sys.executable)" 2>/dev/null || echo python)
SVC_SCRIPT := $(CURDIR)/scripts/start_server.py

install-service:
	$(NSSM_DEV) stop   collmgm-server 2>/dev/null; true
	$(NSSM_DEV) remove collmgm-server confirm 2>/dev/null; true
	$(NSSM_DEV) install collmgm-server "$(SVC_PYTHON)"
	$(NSSM_DEV) set collmgm-server AppParameters      "$(SVC_SCRIPT)"
	$(NSSM_DEV) set collmgm-server AppDirectory       "$(CURDIR)"
	$(NSSM_DEV) set collmgm-server DisplayName        "CollMgm Web Server"
	$(NSSM_DEV) set collmgm-server Start              SERVICE_AUTO_START
	$(NSSM_DEV) start collmgm-server
	netsh advfirewall firewall delete rule name="CollMgm Web Server" 2>/dev/null; true
	netsh advfirewall firewall add rule name="CollMgm Web Server" protocol=TCP dir=in localport=8100 action=allow profile=domain,private

uninstall-service:
	$(NSSM_DEV) stop   collmgm-server 2>/dev/null; true
	$(NSSM_DEV) remove collmgm-server confirm 2>/dev/null; true
	netsh advfirewall firewall delete rule name="CollMgm Web Server" 2>/dev/null; true

clean:
	rm -rf "$(DIST)"
