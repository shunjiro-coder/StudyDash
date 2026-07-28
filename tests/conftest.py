"""Test foundation for StudyDash.

- Points the DB at a throwaway file via STUDYDASH_DB **before** importing db, so
  the real study.db is never touched (C11).
- `fresh_db` (autouse) gives every test a brand-new, schema-initialized DB.
- `clock` freezes db.now_dt() — the single datetime chokepoint — so all
  time-dependent logic (srs / priority) is deterministic.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# Must be set before importing db (db.DB_PATH is read at import).
os.environ["STUDYDASH_DB"] = os.path.join(
    tempfile.gettempdir(), "studydash-test-boot.db")

import db  # noqa: E402

# The fixed "now" every test sees unless it advances the clock.
FIXED_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def _reset_conn():
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        db._local.conn = None


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """A clean DB file per test."""
    old = db.DB_PATH
    db.DB_PATH = str(tmp_path / "study.db")
    _reset_conn()
    db.init()
    yield
    _reset_conn()
    db.DB_PATH = old


class Clock:
    """Callable stand-in for db.now_dt with an advanceable value."""

    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def set(self, dt):
        self.dt = dt

    def advance(self, **kw):
        self.dt = self.dt + timedelta(**kw)
        return self.dt


@pytest.fixture
def clock(monkeypatch):
    c = Clock(FIXED_NOW)
    monkeypatch.setattr(db, "now_dt", c)
    return c
