# Path to Inno Setup compiler — override if installed elsewhere
ISCC       ?= C:/Users/s3teq/AppData/Local/Programs/Inno Setup 6/ISCC.exe
# Release label passed to installer (alpha, beta, ...)
RELEASE    ?= alpha

ISS        := packaging/setup.iss
DIST       := packaging/dist

.DEFAULT_GOAL := help

.PHONY: help build run clean

help:
	@echo "Usage: make [target] [RELEASE=alpha] [STAMP=yyyymmddhhmmss]"
	@echo
	@echo "  build   Build the Windows installer EXE"
	@echo "          RELEASE=alpha (default) -- release label (alpha, beta, ...)"
	@echo "          STAMP=<stamp> -- override build stamp (default: current time)"
	@echo "          Repo is tagged build/<release>-<stamp> after a successful build"
	@echo "          Output: $(DIST)/CollMgm-<release>-<stamp>-Setup.exe"
	@echo "          Requires: Inno Setup 6 and packaging/python/ populated"
	@echo "  run     Launch the app (uses system Python)"
	@echo "  clean   Delete $(DIST)/"
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

run:
	python -c "import sys; sys.path.insert(0, 'scripts'); import collmenu; collmenu.main()"

clean:
	rm -rf "$(DIST)"
