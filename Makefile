# Path to Inno Setup compiler — override if installed elsewhere
ISCC       ?= C:/Program Files (x86)/Inno Setup 6/ISCC.exe
# Release label passed to installer (alpha, beta, ...)
RELEASE    ?= alpha

ISS        := packaging/setup.iss
DIST       := packaging/dist
BUILD_FILE := packaging/build_number.txt

.DEFAULT_GOAL := help

.PHONY: help build run clean

help:
	@echo Usage: make [target] [RELEASE=alpha]
	@echo.
	@echo   build   Build the Windows installer EXE
	@echo           RELEASE=alpha (default) -- release label (alpha, beta, ...)
	@echo           Build number auto-increments from $(BUILD_FILE)
	@echo           Output: $(DIST)/CollMgm-^<release^>-Build^<N^>-Setup.exe
	@echo           Requires: Inno Setup 6 and packaging/python/ populated
	@echo   run     Launch the app (uses system Python)
	@echo   clean   Delete $(DIST)/

build:
	@set -e; \
	BUILD=$$(cat $(BUILD_FILE) 2>/dev/null || echo 0); \
	BUILD=$$((BUILD + 1)); \
	echo $$BUILD > $(BUILD_FILE); \
	echo Building $(RELEASE) build $$BUILD...; \
	"$(ISCC)" /DMyAppRelease=$(RELEASE) /DMyAppBuild=$$BUILD "$(ISS)"

run:
	python -c "import sys; sys.path.insert(0, 'scripts'); import collmenu; collmenu.main()"

clean:
	rm -rf "$(DIST)"
