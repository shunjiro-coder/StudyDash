"""Backup subsystem: snapshot integrity, retention, debounce, restore drill."""

import os
import sqlite3

import backup
import db
from tests import seed


def _point_backup_dir(monkeypatch, tmp_path):
    d = str(tmp_path / "backups")
    monkeypatch.setattr(backup, "BACKUP_DIR", d)
    return d


def test_backup_captures_current_data(clock, monkeypatch, tmp_path):
    _point_backup_dir(monkeypatch, tmp_path)
    co = seed.make_course("数学", "stem")
    seed.make_card(co, "Q", "A")
    path = backup.create_backup()
    assert os.path.exists(path)
    # the snapshot is a real, queryable DB with our row in it
    conn = sqlite3.connect(path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_retention_prunes_old_generations(clock, monkeypatch, tmp_path):
    _point_backup_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(backup, "KEEP", 3)
    # distinct filenames via advancing the (frozen) clock by seconds
    for _ in range(5):
        backup.create_backup()
        clock.advance(seconds=1)
    assert len(backup._backups()) == 3


def test_debounce_one_per_calendar_day(clock, monkeypatch, tmp_path):
    _point_backup_dir(monkeypatch, tmp_path)
    assert backup._has_backup_today() is False
    backup.create_backup()
    assert backup._has_backup_today() is True   # scheduler would skip a 2nd


def test_status_reports_health(clock, monkeypatch, tmp_path):
    _point_backup_dir(monkeypatch, tmp_path)
    assert backup.status() == {"latest": None, "age_days": None}
    backup.create_backup()
    st = backup.status()
    assert st["latest"] is not None
    assert st["age_days"] == 0


def test_restore_drill(clock, monkeypatch, tmp_path):
    """Snapshot -> mutate live DB -> restore snapshot over it -> old data back."""
    _point_backup_dir(monkeypatch, tmp_path)
    co = seed.make_course()
    seed.make_card(co, "keep", "me")
    snap = backup.create_backup()

    # mutate after the snapshot
    seed.make_card(co, "added", "later")
    assert db.query_one("SELECT COUNT(*) n FROM cards")["n"] == 2

    # restore: replace the live file with the snapshot (WAL cleared by fixture's
    # fresh connection on next use)
    from tests.conftest import _reset_conn
    _reset_conn()
    import shutil
    shutil.copyfile(snap, db.DB_PATH)
    for ext in ("-wal", "-shm"):
        p = db.DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)
    _reset_conn()

    assert db.query_one("SELECT COUNT(*) n FROM cards")["n"] == 1
    assert db.query_one("SELECT front FROM cards")["front"] == "keep"
