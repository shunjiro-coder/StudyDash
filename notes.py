"""Notes / outliner API (Phase B1): docs -- rems.

RemNote-style outline: a doc holds a tree of rems (bullets). Ordering uses
fractional indexing — position is a REAL and a new/moved rem lands at the midpoint
between its neighbours, so one insert/move rewrites ONE row, not the whole list.
When float precision between two neighbours runs out, that sibling group is
renormalized to integer spacing (1,2,3,…) and the new positions are returned so
the client re-syncs (a stale client position can't then collide with a fresh one).

All new routes live on a Blueprint (notes_bp), registered in app.py — the app-wide
before_request token guard and JSON error handlers apply automatically (guardrail 6,
plan §3.3). Card materialization from rems (delimiters / cloze / occlusion) is NOT
here on purpose: it arrives in Phase C, deliberately OUT of the autosave path (B4).
Deleting a doc/rem here just CASCADE-deletes rems; the "suspend derived cards first"
step (D-1) also lands in C, once cards.rem_id exists.

Times: created_at/updated_at are D-4 UTC. docs.daily_date is a LOCAL calendar-day
label ('YYYY-MM-DD') — how a human names "today's note", not a UTC instant.
"""

import json

from flask import Blueprint, abort, jsonify, request

import db

notes_bp = Blueprint("notes", __name__)

VALID_REM_TYPES = {
    "bullet", "heading", "todo", "code", "quote", "divider",
    "image", "latex", "table", "portal",
}


# --------------------------------------------------------------------------
# Serializers
# --------------------------------------------------------------------------
def doc_dict(r, extra=None):
    keys = r.keys()
    d = {
        "id": r["id"], "title": r["title"], "course_id": r["course_id"],
        "is_daily": bool(r["is_daily"]), "daily_date": r["daily_date"],
        "archived": bool(r["archived"]),
        # Phase I: which folder the note lives in (NULL = 未分類). The column is
        # added by an additive migration, so tolerate rows read before it exists.
        "folder_id": r["folder_id"] if "folder_id" in keys else None,
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }
    if extra:
        d.update(extra)
    return d


def folder_dict(r, doc_count=None):
    d = {"id": r["id"], "name": r["name"], "position": r["position"],
         "created_at": r["created_at"]}
    if doc_count is not None:
        d["doc_count"] = doc_count
    return d


def rem_dict(r):
    return {
        "id": r["id"], "doc_id": r["doc_id"], "parent_id": r["parent_id"],
        "position": r["position"], "text": r["text"], "rem_type": r["rem_type"],
        "props": json.loads(r["props_json"]) if r["props_json"] else None,
        "collapsed": bool(r["collapsed"]), "done": bool(r["done"]),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def _touch_doc(doc_id, now):
    if doc_id:
        db.write("UPDATE docs SET updated_at=? WHERE id=?", (now, doc_id))


def _doc_with_rems(row):
    rems = db.query(
        "SELECT * FROM rems WHERE doc_id=? ORDER BY position, id", (row["id"],))
    return doc_dict(row, {"rems": [rem_dict(r) for r in rems]})


# --------------------------------------------------------------------------
# Fractional indexing + tree helpers
# --------------------------------------------------------------------------
def _siblings(doc_id, parent_id, exclude_id=None):
    """Ordered siblings under (doc_id, parent_id). `parent_id IS ?` matches both a
    real parent and NULL (top level). `exclude_id` drops the moving rem itself."""
    return db.query(
        "SELECT id, position FROM rems WHERE doc_id=? AND parent_id IS ? "
        "AND (? IS NULL OR id != ?) ORDER BY position, id",
        (doc_id, parent_id, exclude_id, exclude_id))


def _compute_position(doc_id, parent_id, after_id, exclude_id=None):
    """Position for a rem placed after `after_id` (None = at the very start).
    Returns None when float precision between the neighbours is exhausted; the
    caller then renormalizes and retries."""
    sibs = _siblings(doc_id, parent_id, exclude_id)
    if not sibs:
        return 1.0
    if after_id is None:
        first = sibs[0]["position"]
        mid = first / 2.0
        return None if (mid <= 0.0 or mid >= first) else mid
    idx = next((i for i, s in enumerate(sibs) if s["id"] == after_id), None)
    if idx is None:                       # after_id isn't a sibling -> append
        return sibs[-1]["position"] + 1.0
    a = sibs[idx]["position"]
    if idx + 1 >= len(sibs):              # after the last -> append
        return a + 1.0
    b = sibs[idx + 1]["position"]
    mid = (a + b) / 2.0
    return None if (mid <= a or mid >= b) else mid


def _renormalize(doc_id, parent_id):
    """Rewrite a sibling group to integer spacing (1,2,3,…). Returns the new
    [{id, position}] so the client can re-sync any stale positions it holds."""
    sibs = _siblings(doc_id, parent_id)
    stmts = [("UPDATE rems SET position=? WHERE id=?", (float(i + 1), s["id"]))
             for i, s in enumerate(sibs)]
    if stmts:
        db.write_many(stmts)
    return [{"id": s["id"], "position": float(i + 1)}
            for i, s in enumerate(sibs)]


def _place(doc_id, parent_id, after_id, exclude_id=None):
    """(position, renormalized_or_None) for a rem placed after `after_id`."""
    pos = _compute_position(doc_id, parent_id, after_id, exclude_id)
    renorm = None
    if pos is None:
        renorm = _renormalize(doc_id, parent_id)
        pos = _compute_position(doc_id, parent_id, after_id, exclude_id)
        if pos is None:                   # defensive: after_id was the tail
            pos = (renorm[-1]["position"] + 1.0) if renorm else 1.0
    return pos, renorm


def _subtree_ids(rem_id):
    """rem_id plus every descendant id (BFS). Carries a `seen` set (like
    _ancestors / _is_descendant) so a cyclic parent chain — reachable only via a
    reorder race, but also any pre-existing bad data — terminates instead of
    looping forever and OOMing."""
    ids, seen, stack = [], set(), [rem_id]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        ids.append(pid)
        for c in db.query("SELECT id FROM rems WHERE parent_id=?", (pid,)):
            stack.append(c["id"])
    return ids


def _ancestors(rem_id):
    """Breadcrumb: ordered [root … parent] above rem_id (guarded against cycles)."""
    chain, seen = [], set()
    row = db.query_one("SELECT id, parent_id, text FROM rems WHERE id=?", (rem_id,))
    while row and row["parent_id"] is not None and row["parent_id"] not in seen:
        seen.add(row["parent_id"])
        p = db.query_one("SELECT id, parent_id, text FROM rems WHERE id=?",
                         (row["parent_id"],))
        if not p:
            break
        chain.append({"id": p["id"], "text": p["text"]})
        row = p
    chain.reverse()
    return chain


def _is_descendant(candidate, rem_id):
    """True if `candidate` is rem_id itself or lies inside rem_id's subtree — i.e.
    moving rem_id under `candidate` would create a cycle."""
    cur, seen = candidate, set()
    while cur is not None and cur not in seen:
        if cur == rem_id:
            return True
        seen.add(cur)
        row = db.query_one("SELECT parent_id FROM rems WHERE id=?", (cur,))
        cur = row["parent_id"] if row else None
    return False


# --------------------------------------------------------------------------
# Docs
# --------------------------------------------------------------------------
@notes_bp.route("/api/docs")
def api_docs():
    sql = "SELECT * FROM docs"
    if request.args.get("archived") != "1":
        sql += " WHERE archived=0"
    sql += " ORDER BY is_daily DESC, updated_at DESC, id DESC"
    return jsonify([doc_dict(r) for r in db.query(sql)])


@notes_bp.route("/api/docs", methods=["POST"])
def api_doc_create():
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    now = db.now_utc_iso()
    new_id = db.write(
        "INSERT INTO docs (title, course_id, created_at, updated_at) "
        "VALUES (?,?,?,?)", (title, data.get("course_id"), now, now))
    return jsonify(doc_dict(
        db.query_one("SELECT * FROM docs WHERE id=?", (new_id,)))), 201


@notes_bp.route("/api/docs/daily")
def api_docs_daily():
    # Local calendar day = the label a human gives "today's note" (NOT a D-4 UTC
    # stamp). get-or-create is race-safe: ON CONFLICT DO NOTHING (against the
    # partial unique index idx_docs_daily) then SELECT — two tabs at once can't
    # 500 or duplicate. Writes serialize through db.py's process-wide lock.
    today = db.now_dt().astimezone().strftime("%Y-%m-%d")
    now = db.now_utc_iso()
    db.write(
        "INSERT INTO docs (title, is_daily, daily_date, created_at, updated_at) "
        "VALUES (?,1,?,?,?) ON CONFLICT DO NOTHING", (today, today, now, now))
    row = db.query_one(
        "SELECT * FROM docs WHERE is_daily=1 AND daily_date=?", (today,))
    return jsonify(_doc_with_rems(row))


@notes_bp.route("/api/docs/<int:did>")
def api_doc_detail(did):
    row = db.query_one("SELECT * FROM docs WHERE id=?", (did,))
    if not row:
        abort(404)
    return jsonify(_doc_with_rems(row))


@notes_bp.route("/api/docs/<int:did>", methods=["PATCH"])
def api_doc_update(did):
    data = request.get_json(force=True, silent=True) or {}
    if not db.query_one("SELECT id FROM docs WHERE id=?", (did,)):
        abort(404)
    fields, params = [], []
    if "title" in data:
        fields.append("title=?")
        params.append((data["title"] or "").strip())
    if "course_id" in data:
        fields.append("course_id=?")
        params.append(data["course_id"])
    if "folder_id" in data:
        # Phase I: move a note into a folder (or NULL to un-file). Validate the
        # target exists so a stale id can't orphan the note into a phantom folder.
        fid = data["folder_id"]
        if fid is not None and not db.query_one(
                "SELECT id FROM folders WHERE id=?", (fid,)):
            abort(400, "unknown folder")
        fields.append("folder_id=?")
        params.append(fid)
    if "archived" in data:
        fields.append("archived=?")
        params.append(1 if data["archived"] else 0)
    if not fields:
        abort(400, "nothing to update")
    fields.append("updated_at=?")
    params.append(db.now_utc_iso())
    params.append(did)
    db.write(f"UPDATE docs SET {', '.join(fields)} WHERE id=?", params)
    return jsonify(doc_dict(db.query_one("SELECT * FROM docs WHERE id=?", (did,))))


@notes_bp.route("/api/docs/<int:did>", methods=["DELETE"])
def api_doc_delete(did):
    if not db.query_one("SELECT id FROM docs WHERE id=?", (did,)):
        abort(404)
    # Phase C will suspend cards derived from this doc's rems BEFORE deleting
    # (cards.rem_id doesn't exist yet). rems CASCADE via FK.
    db.write("DELETE FROM docs WHERE id=?", (did,))
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Folders (Phase I) — user-named containers to organize notes. A note has at most
# one folder (docs.folder_id); NULL = 未分類. Deleting a folder un-files its notes.
# --------------------------------------------------------------------------
@notes_bp.route("/api/folders")
def api_folders():
    rows = db.query("SELECT * FROM folders ORDER BY position, id")
    counts = {r["folder_id"]: r["n"] for r in db.query(
        "SELECT folder_id, COUNT(*) n FROM docs "
        "WHERE archived=0 AND folder_id IS NOT NULL GROUP BY folder_id")}
    return jsonify([folder_dict(r, counts.get(r["id"], 0)) for r in rows])


@notes_bp.route("/api/folders", methods=["POST"])
def api_folder_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        abort(400, "name required")
    now = db.now_utc_iso()
    # Append at the end (max position + 1) so new folders sort last, stably.
    mx = db.query_one("SELECT COALESCE(MAX(position), 0) mx FROM folders")["mx"]
    new_id = db.write(
        "INSERT INTO folders (name, position, created_at) VALUES (?,?,?)",
        (name, mx + 1.0, now))
    return jsonify(folder_dict(
        db.query_one("SELECT * FROM folders WHERE id=?", (new_id,)), 0)), 201


@notes_bp.route("/api/folders/<int:fid>", methods=["PATCH"])
def api_folder_update(fid):
    data = request.get_json(force=True, silent=True) or {}
    if not db.query_one("SELECT id FROM folders WHERE id=?", (fid,)):
        abort(404)
    fields, params = [], []
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            abort(400, "name cannot be empty")
        fields.append("name=?")
        params.append(name)
    if "position" in data:
        # Guard the coercion so a non-numeric/null position is a clean 400, not a
        # 500 (mirrors the count-parse guard in app.py's quiz endpoint).
        try:
            pos = float(data["position"])
        except (TypeError, ValueError):
            abort(400, "position must be a number")
        fields.append("position=?")
        params.append(pos)
    if not fields:
        abort(400, "nothing to update")
    params.append(fid)
    db.write(f"UPDATE folders SET {', '.join(fields)} WHERE id=?", params)
    return jsonify(folder_dict(db.query_one("SELECT * FROM folders WHERE id=?", (fid,))))


@notes_bp.route("/api/folders/<int:fid>", methods=["DELETE"])
def api_folder_delete(fid):
    if not db.query_one("SELECT id FROM folders WHERE id=?", (fid,)):
        abort(404)
    # Un-file this folder's notes (folder_id -> NULL) BEFORE deleting the folder,
    # so notes survive as 未分類 rather than pointing at a phantom folder (we don't
    # rely on FK cascade — the foreign_keys pragma may be off).
    db.write_many([
        ("UPDATE docs SET folder_id=NULL WHERE folder_id=?", (fid,)),
        ("DELETE FROM folders WHERE id=?", (fid,)),
    ])
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Rems
# --------------------------------------------------------------------------
@notes_bp.route("/api/rems", methods=["POST"])
def api_rem_create():
    data = request.get_json(force=True, silent=True) or {}
    doc_id = data.get("doc_id")
    if not doc_id or not db.query_one("SELECT id FROM docs WHERE id=?", (doc_id,)):
        abort(400, "valid doc_id required")
    parent_id = data.get("parent_id")
    if parent_id is not None and not db.query_one(
            "SELECT id FROM rems WHERE id=? AND doc_id=?", (parent_id, doc_id)):
        abort(400, "parent_id not in this doc")
    rem_type = data.get("rem_type") or "bullet"
    if rem_type not in VALID_REM_TYPES:
        rem_type = "bullet"
    pos, renorm = _place(doc_id, parent_id, data.get("after_id"))
    now = db.now_utc_iso()
    new_id = db.write(
        "INSERT INTO rems (doc_id, parent_id, position, text, rem_type, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (doc_id, parent_id, pos, (data.get("text") or ""), rem_type, now, now))
    _touch_doc(doc_id, now)
    out = rem_dict(db.query_one("SELECT * FROM rems WHERE id=?", (new_id,)))
    return jsonify({"rem": out, "renormalized": renorm}), 201


@notes_bp.route("/api/rems/<int:rid>", methods=["PATCH"])
def api_rem_update(rid):
    data = request.get_json(force=True, silent=True) or {}
    cur = db.query_one("SELECT * FROM rems WHERE id=?", (rid,))
    if not cur:
        abort(404)
    fields, params = [], []
    if "text" in data:
        fields.append("text=?")
        params.append(data["text"] or "")
    if "rem_type" in data and data["rem_type"] in VALID_REM_TYPES:
        fields.append("rem_type=?")
        params.append(data["rem_type"])
    if "collapsed" in data:
        fields.append("collapsed=?")
        params.append(1 if data["collapsed"] else 0)
    if "done" in data:
        fields.append("done=?")
        params.append(1 if data["done"] else 0)
    if "props" in data:
        fields.append("props_json=?")
        params.append(json.dumps(data["props"]) if data["props"] is not None else None)
    if not fields:
        abort(400, "nothing to update")
    now = db.now_utc_iso()
    fields.append("updated_at=?")
    params.append(now)
    params.append(rid)
    db.write(f"UPDATE rems SET {', '.join(fields)} WHERE id=?", params)
    _touch_doc(cur["doc_id"], now)
    return jsonify(rem_dict(db.query_one("SELECT * FROM rems WHERE id=?", (rid,))))


@notes_bp.route("/api/rems/<int:rid>", methods=["DELETE"])
def api_rem_delete(rid):
    cur = db.query_one("SELECT * FROM rems WHERE id=?", (rid,))
    if not cur:
        abort(404)
    # Phase C: suspend derived cards before delete (cards.rem_id lands in C).
    db.write("DELETE FROM rems WHERE id=?", (rid,))   # subtree CASCADEs
    _touch_doc(cur["doc_id"], db.now_utc_iso())
    return jsonify({"ok": True})


@notes_bp.route("/api/rems/reorder", methods=["POST"])
def api_rem_reorder():
    """Move a rem: indent/outdent/DnD all reduce to {rem_id, new_parent_id, after_id}."""
    data = request.get_json(force=True, silent=True) or {}
    rid = data.get("rem_id")
    cur = db.query_one("SELECT * FROM rems WHERE id=?", (rid,)) if rid else None
    if not cur:
        abort(404 if rid else 400)
    doc_id = cur["doc_id"]
    new_parent = data.get("new_parent_id")
    after_id = data.get("after_id")
    if new_parent is not None and not db.query_one(
            "SELECT id FROM rems WHERE id=? AND doc_id=?", (new_parent, doc_id)):
        abort(400, "new_parent_id not in this doc")
    now = db.now_utc_iso()
    # The cycle guard and the reparent write must be ATOMIC. Reads are lock-free
    # under WAL and only writes take db._write_lock, so a lock-free check-then-write
    # lets two concurrent opposite moves (P->Q while Q->P) both pass and create a
    # cycle (TOCTOU). Hold the (reentrant) write lock across the re-check + UPDATE:
    # the loser then re-reads the winner's committed reparent and aborts. _place()
    # and db.write() below re-acquire the RLock fine.
    with db._write_lock:
        if new_parent is not None and _is_descendant(new_parent, rid):
            abort(400, "cannot move a rem into its own subtree")
        pos, renorm = _place(doc_id, new_parent, after_id, exclude_id=rid)
        db.write("UPDATE rems SET parent_id=?, position=?, updated_at=? WHERE id=?",
                 (new_parent, pos, now, rid))
    _touch_doc(doc_id, now)
    out = rem_dict(db.query_one("SELECT * FROM rems WHERE id=?", (rid,)))
    return jsonify({"rem": out, "renormalized": renorm})


@notes_bp.route("/api/rems/batch", methods=["POST"])
def api_rem_batch():
    """Autosave: persist dirty rem TEXT atomically (write_many). Text only — no
    structural change (that goes through create/reorder). Card materialization is
    deliberately NOT triggered here (Phase C runs it on a separate path, B4)."""
    data = request.get_json(force=True, silent=True) or {}
    now = db.now_utc_iso()
    stmts = []
    for it in (data.get("items") or []):
        rid = it.get("id")
        if rid is None:
            continue
        stmts.append(("UPDATE rems SET text=?, updated_at=? WHERE id=?",
                      (it.get("text") or "", now, rid)))
    if stmts:
        db.write_many(stmts)
    _touch_doc(data.get("doc_id"), now)
    return jsonify({"ok": True, "saved": len(stmts), "updated_at": now})


# --------------------------------------------------------------------------
# Search (Phase J) — the sidebar 🔍 tab. A simple substring search over note
# titles + rem text (no external index / FTS extension — dependency-free), each
# matching doc returned once with a snippet and hit count.
# --------------------------------------------------------------------------
def _snippet(text, q, radius=42):
    text = text or ""
    i = text.lower().find(q.lower())
    if i < 0:
        return text[:90] + ("…" if len(text) > 90 else "")
    start, end = max(0, i - radius), min(len(text), i + len(q) + radius)
    s = text[start:end]
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


@notes_bp.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"query": "", "docs": []})
    # LIKE with the wildcard/escape chars in q neutralized (treat q literally).
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{esc}%"
    docs = db.query(
        "SELECT DISTINCT d.* FROM docs d LEFT JOIN rems r ON r.doc_id = d.id "
        "WHERE d.archived=0 AND (d.title LIKE ? ESCAPE '\\' OR r.text LIKE ? ESCAPE '\\') "
        "ORDER BY d.is_daily DESC, d.updated_at DESC LIMIT 50", (like, like))
    out = []
    for d in docs:
        hit = db.query_one(
            "SELECT text FROM rems WHERE doc_id=? AND text LIKE ? ESCAPE '\\' "
            "ORDER BY position, id LIMIT 1", (d["id"], like))
        n = db.query_one(
            "SELECT COUNT(*) n FROM rems WHERE doc_id=? AND text LIKE ? ESCAPE '\\'",
            (d["id"], like))["n"]
        out.append(doc_dict(d, {
            "snippet": _snippet(hit["text"], q) if hit else None,
            "match_count": n}))
    return jsonify({"query": q, "docs": out})


@notes_bp.route("/api/rems/<int:rid>")
def api_rem_zoom(rid):
    """Zoom into a rem: its subtree (ordered) + the ancestor breadcrumb above it."""
    cur = db.query_one("SELECT * FROM rems WHERE id=?", (rid,))
    if not cur:
        abort(404)
    ids = _subtree_ids(rid)
    qmarks = ",".join("?" * len(ids))
    rems = db.query(
        f"SELECT * FROM rems WHERE id IN ({qmarks}) ORDER BY position, id", ids)
    return jsonify({
        "root": rem_dict(cur),
        "breadcrumb": _ancestors(rid),
        "rems": [rem_dict(r) for r in rems],
    })
