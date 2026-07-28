"""Background job queue (single worker thread).

One worker thread processes jobs serially. This:
- caps concurrent `claude -p` spawns at 1 (well within the 1-2 limit; each
  spawn is 2-6s startup and ~255MB), and
- complements db.py's write lock (analysis writes never collide).

Handlers register themselves at import time (ingest.py, generate.py), so this
module imports nothing from them (no circular import).

Boot reconcile: the queue is in-memory, so a crash/restart (LaunchAgent
KeepAlive) would leave materials stuck 'extracting'/'generating' forever.
reconcile() re-enqueues them at startup.
"""

import queue
import threading
import traceback

_q = queue.Queue()
_worker = None
_lock = threading.Lock()
_handlers = {}


def register(job_type, fn):
    _handlers[job_type] = fn


def enqueue(job_type, payload):
    _q.put((job_type, payload))


def _run():
    while True:
        job_type, payload = _q.get()
        try:
            handler = _handlers.get(job_type)
            if handler is None:
                print(f"[worker] no handler for job '{job_type}'")
            else:
                handler(payload)
        except Exception:  # noqa: BLE001 — a bad job must not kill the worker
            print(f"[worker] job '{job_type}' failed:\n{traceback.format_exc()}")
        finally:
            _q.task_done()


def start():
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="studydash-worker",
                                       daemon=True)
            _worker.start()


def reconcile():
    """Re-enqueue materials left mid-analysis by a previous crash/restart."""
    import db
    rows = db.query(
        "SELECT id, status FROM materials "
        "WHERE status IN ('extracting','generating')")
    for r in rows:
        if r["status"] == "generating":
            enqueue("generate", {"material_id": r["id"]})
        else:
            enqueue("ingest", {"material_id": r["id"]})
    if rows:
        print(f"[worker] reconciled {len(rows)} stuck material(s)")
    return len(rows)
