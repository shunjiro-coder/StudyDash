# StudyDash dev tasks. Uses the project venv so nothing depends on global tools.
PY := ./venv/bin/python

.PHONY: test lint fmt backup run

test:                 ## run the test suite (clock-frozen srs/priority + behavior)
	$(PY) -m pytest -q

lint:                 ## ruff check (lint) — the one cheap quality win
	$(PY) -m ruff check .

fmt:                  ## ruff format (apply)
	$(PY) -m ruff format .

backup:               ## take one on-demand study.db snapshot into backups/
	$(PY) -c "import backup; print(backup.create_backup())"

run:                  ## start the app (127.0.0.1:5000)
	$(PY) app.py
