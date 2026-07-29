"""study.db online backup (feature-safe, zero new dependencies).

The single most valuable safety net: study.db holds all grades, cards and review
history and is NOT in git (.gitignore). A corrupt WAL or a bad restore could lose
everything. sqlite3's online `.backup()` API is WAL-safe (consistent snapshot
without stopping the app).

Scheduling: a daily, debounced timer — NOT a backup-on-every-boot (a crash loop
would flood) and NOT a one-shot-at-startup (a long-running process would back up
zero times after boot). We check hourly and take at most one backup per local
calendar day, keeping the last KEEP generations.

Restore drill (the WAL foot-gun):
    1. stop the app  (launchctl unload … / Ctrl-C)
    2. cp backups/study-YYYYMMDD-HHMMSS.db study.db
    3. rm -f study.db-wal study.db-shm      # stale WAL would clobber the restore
    4. start the app
"""

import glob
import os
import sqlite3
import threading
import traceback

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP = 14          # generations retained (~2 weeks of daily backups)
PREFIX = "study-"
SUFFIX = ".db"


def _stamp():
    # local-time stamp so a human reading the filename sees their own day
    return db.now_dt().astimezone().strftime("%Y%m%d-%H%M%S")


def create_backup():
    """Take one consistent snapshot of the live DB. Returns the backup path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"{PREFIX}{_stamp()}{SUFFIX}")
    src = sqlite3.connect(db.DB_PATH, timeout=5.0)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)     # online, WAL-safe snapshot
        finally:
            dst.close()
    finally:
        src.close()
    _prune()
    return dest


def pre_migration_backup(phase):
    """One WAL-safe snapshot before a phase's first schema migration (guardrail 4).

    Distinct from the daily backups/ generations: written as study.db.bak.<phase>
    right next to the live DB so it's obvious what it protects and trivial to
    restore by hand. Uses the online .backup() API (never a file copy — a copy
    could tear a live WAL). Returns the path, or None if there's no DB to protect."""
    if not os.path.exists(db.DB_PATH):
        return None
    dest = f"{db.DB_PATH}.bak.{phase}"
    src = sqlite3.connect(db.DB_PATH, timeout=5.0)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    print(f"[backup] pre-migration snapshot: {dest}")
    return dest


def _backups():
    return sorted(glob.glob(os.path.join(BACKUP_DIR, f"{PREFIX}*{SUFFIX}")))


def _prune():
    for old in _backups()[:-KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass


def latest_backup():
    files = _backups()
    return files[-1] if files else None


def latest_backup_age_days():
    """Whole days since the most recent backup, or None if there are none."""
    latest = latest_backup()
    if not latest:
        return None
    age = db.now_dt().timestamp() - os.path.getmtime(latest)
    return max(0, int(age // 86400))


def status():
    """Passive health signal for /api/meta (surfaces a silently-stopped timer)."""
    latest = latest_backup()
    if not latest:
        return {"latest": None, "age_days": None}
    import datetime
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(latest))
    return {"latest": mtime.astimezone().isoformat(),
            "age_days": latest_backup_age_days()}


def _has_backup_today():
    today = db.now_dt().astimezone().strftime("%Y%m%d")
    return any(os.path.basename(p).startswith(f"{PREFIX}{today}-")
               for p in _backups())


def start_scheduler():
    """Daily, debounced background backups. Idempotent per calendar day."""
    def loop():
        while True:
            try:
                if not _has_backup_today():
                    path = create_backup()
                    print(f"[backup] wrote {path}")
            except Exception:  # noqa: BLE001 — a backup error must not kill the app
                print(f"[backup] failed:\n{traceback.format_exc()}")
            _sleep_hour.wait(3600)   # re-check hourly; debounced by calendar day

    threading.Thread(target=loop, name="studydash-backup", daemon=True).start()


# Event used purely so the hourly sleep is interruptible in tests.
_sleep_hour = threading.Event()
