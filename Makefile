# Path to Inno Setup compiler — override if installed elsewhere
ISCC ?= C:/Program Files (x86)/Inno Setup 6/ISCC.exe

ISS  := packaging/setup.iss
DIST := packaging/dist

.DEFAULT_GOAL := help

.PHONY: help build run clean

help:
	@echo Usage: make [target]
	@echo.
	@echo   build   Build the Windows installer EXE
	@echo           Requires: Inno Setup 6 and packaging/python/ populated
	@echo           Output:   $(DIST)/CollMgm-Beta-Setup.exe
	@echo   run     Launch the app (uses system Python)
	@echo   clean   Delete $(DIST)/

build:
	"$(ISCC)" "$(ISS)"

run:
	python -c "import sys; sys.path.insert(0, 'scripts'); import collmenu; collmenu.main()"

clean:
	rm -rf "$(DIST)"
