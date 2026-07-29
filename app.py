"""StudyDash Flask app — entry point + routing.

Runs on 127.0.0.1 by default (single-user local dashboard). Times are stored
UTC (D-4) and sent to the browser as raw ISO8601; the browser formats to local.

Concurrency: writes are serialized in db.py (process-wide lock); Flask runs
threaded with the reloader OFF (reloader would double-spawn schedulers/threads).
"""

import json
import os
import secrets

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

import ai
import calendar_export
import db
import generate  # noqa: F401 — registers the 'generate' worker handler at import
import ingest  # noqa: F401 — registers the 'ingest' worker handler at import
import notes
import srs
import worker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")

app = Flask(__name__, template_folder="templates", static_folder="static")

# Absolute request-size backstop. Per-file limits live in ingest.process_upload
# (so one oversized photo lands in errors[] instead of 413-ing the whole batch);
# this only guards against an absurdly large request body.
app.config["MAX_CONTENT_LENGTH"] = int(
    db.load_settings().get("max_request_mb", 200)) * 1024 * 1024


# JSON error bodies (400/404/413/500 -> {error, detail}). The frontend api()
# branches on res.ok and shows the body as text, so this is contract-safe.
@app.errorhandler(HTTPException)
def _json_http_error(e):
    return jsonify({"error": e.name, "detail": e.description}), e.code


@app.errorhandler(Exception)
def _json_unexpected_error(e):
    # last-resort 500 as JSON; don't leak internals beyond the exception type
    return jsonify({"error": "Internal Server Error",
                    "detail": type(e).__name__}), 500

# Public-mode (host=0.0.0.0) shared-token guard. Flask has no auth; on an open
# 0.0.0.0 bind anyone on the LAN could read grades/photos and hit /api/upload to
# burn ~$0.05/call of claude budget. So when public, everything requires a token
# (?key=... once -> cookie). On 127.0.0.1 (default) this guard is inert.
PUBLIC = False
SHARE_TOKEN = None


@app.before_request
def _public_guard():
    if not PUBLIC:
        return
    provided = (request.args.get("key") or request.cookies.get("sdkey")
                or request.headers.get("X-StudyDash-Key"))
    if SHARE_TOKEN and provided == SHARE_TOKEN:
        request.environ["_sd_set_cookie"] = bool(request.args.get("key"))
        return
    abort(401, "shared token required (append ?key=... to the URL)")


@app.after_request
def _set_cookie(resp):
    if request.environ.get("_sd_set_cookie"):
        resp.set_cookie("sdkey", SHARE_TOKEN, httponly=True, samesite="Lax")
    return resp


# Notes/outliner API (Phase B1). Registered at import (not in bootstrap()) so the
# test client, which never calls bootstrap(), still sees these routes. The app-wide
# before_request token guard + JSON error handlers apply to it automatically.
app.register_blueprint(notes.notes_bp)


# Serialization lives in db.py (shared with priority.py). Send raw UTC ISO;
# the browser localizes.
from db import assignment_dict, course_dict  # noqa: E402

ASSIGN_SELECT = db.ASSIGN_JOIN


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------
# Courses
# --------------------------------------------------------------------------
@app.route("/api/courses")
def api_courses():
    rows = db.query("SELECT * FROM courses ORDER BY name")
    return jsonify([course_dict(r) for r in rows])


@app.route("/api/courses/<int:cid>", methods=["PATCH"])
def api_course_update(cid):
    data = request.get_json(force=True, silent=True) or {}
    fields, params = [], []
    if "subject_type" in data:
        if data["subject_type"] not in ("stem", "memo", "lang", "other"):
            abort(400, "bad subject_type")
        fields.append("subject_type = ?")
        params.append(data["subject_type"])
    if "weights" in data:
        fields.append("weight_config_json = ?")
        params.append(json.dumps(data["weights"]) if data["weights"] else None)
    if not fields:
        abort(400, "nothing to update")
    params.append(cid)
    db.write(f"UPDATE courses SET {', '.join(fields)} WHERE id = ?", params)
    row = db.query_one("SELECT * FROM courses WHERE id = ?", (cid,))
    return jsonify(course_dict(row))


# --------------------------------------------------------------------------
# Assignments — CRUD
# --------------------------------------------------------------------------
VALID_STATUS = {"proposed", "todo", "in_progress", "submitted", "graded"}
VALID_CATEGORY = {"homework", "quiz", "exam", "project", "other"}


@app.route("/api/assignments")
def api_assignments():
    where, params = [], []
    status = request.args.get("status")
    course = request.args.get("course_id")
    if status:
        where.append("a.status = ?")
        params.append(status)
    if course:
        where.append("a.course_id = ?")
        params.append(course)
    sql = ASSIGN_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.pinned DESC, (a.due_at IS NULL), a.due_at ASC"
    rows = db.query(sql, params)
    return jsonify([assignment_dict(r) for r in rows])


@app.route("/api/assignments", methods=["POST"])
def api_assignment_create():
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        abort(400, "title required")

    course_id = data.get("course_id")
    course_name = (data.get("course_name") or "").strip()
    if not course_id and course_name:
        course_id = db.get_or_create_course(course_name)

    category = data.get("category") or "homework"
    if category not in VALID_CATEGORY:
        category = "homework"
    est = data.get("estimated_minutes")
    if est not in (15, 60, 180):
        est = 60

    # D-1: category filled at creation. D-4: due_at is UTC ISO (browser sends
    # an ISO string with offset, or null). external_id stays NULL for manual
    # (never '' — that would collapse all manual rows under UNIQUE).
    now = db.now_utc_iso()
    new_id = db.write(
        """
        INSERT INTO assignments
            (course_id, title, description, due_at, category, max_points,
             status, estimated_minutes, source, external_id,
             updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', NULL, ?, ?)
        """,
        (course_id, title, data.get("description"),
         db.normalize_due(data.get("due_at")),
         category, data.get("max_points"), data.get("status") or "todo",
         est, now, now),
    )
    row = db.query_one(ASSIGN_SELECT + " WHERE a.id = ?", (new_id,))
    return jsonify(assignment_dict(row)), 201


@app.route("/api/assignments/<int:aid>", methods=["PATCH"])
def api_assignment_update(aid):
    data = request.get_json(force=True, silent=True) or {}
    cur = db.query_one("SELECT * FROM assignments WHERE id = ?", (aid,))
    if not cur:
        abort(404)
    fields, params = [], []
    simple = {
        "title": str, "description": str, "due_at": str,
        "max_points": float, "earned_points": float,
    }
    for key, cast in simple.items():
        if key in data:
            val = data[key]
            if val is not None and cast in (float,):
                try:
                    val = cast(val)
                except (TypeError, ValueError):
                    val = None
            if key == "due_at":
                # D-4 defensive: normalize any offset to UTC; keep an
                # unparseable-but-present value rather than NULL a deadline.
                val = db.normalize_due(val)
            fields.append(f"{key} = ?")
            params.append(val)
    if "category" in data and data["category"] in VALID_CATEGORY:
        fields.append("category = ?")
        params.append(data["category"])
    if "status" in data and data["status"] in VALID_STATUS:
        fields.append("status = ?")
        params.append(data["status"])
    if "estimated_minutes" in data and data["estimated_minutes"] in (15, 60, 180):
        fields.append("estimated_minutes = ?")
        params.append(data["estimated_minutes"])
    if "pinned" in data:
        fields.append("pinned = ?")
        params.append(1 if data["pinned"] else 0)
    if "course_name" in data and data["course_name"]:
        fields.append("course_id = ?")
        params.append(db.get_or_create_course(data["course_name"]))
    if not fields:
        abort(400, "nothing to update")
    fields.append("updated_at = ?")
    params.append(db.now_utc_iso())
    params.append(aid)
    db.write(f"UPDATE assignments SET {', '.join(fields)} WHERE id = ?", params)
    row = db.query_one(ASSIGN_SELECT + " WHERE a.id = ?", (aid,))
    return jsonify(assignment_dict(row))


@app.route("/api/assignments/<int:aid>", methods=["DELETE"])
def api_assignment_delete(aid):
    db.write("DELETE FROM assignments WHERE id = ?", (aid,))
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Today (Phase 1: overdue isolated + due-ordered. Phase 2 swaps in priority.py)
# --------------------------------------------------------------------------
@app.route("/api/today")
def api_today():
    try:
        import priority
        return jsonify(priority.build_today())
    except ImportError:
        pass
    now = db.now_utc_iso()
    active = "status IN ('todo','in_progress')"
    overdue = db.query(
        ASSIGN_SELECT + f" WHERE {active} AND a.due_at IS NOT NULL "
        "AND a.due_at < ? ORDER BY a.due_at ASC", (now,))
    todo = db.query(
        ASSIGN_SELECT + f" WHERE {active} AND (a.due_at IS NULL OR a.due_at >= ?) "
        "ORDER BY a.pinned DESC, (a.due_at IS NULL), a.due_at ASC", (now,))
    return jsonify({
        "focus": assignment_dict(todo[0]) if todo else None,
        "focus_reason": None,
        "todo": [assignment_dict(r) for r in todo],
        "overdue": [assignment_dict(r) for r in overdue],
        "review_due": 0,
    })


# --------------------------------------------------------------------------
# Classroom sync (graceful — never 500s the UI)
# --------------------------------------------------------------------------
@app.route("/api/sync", methods=["POST"])
def api_sync():
    import classroom
    if not classroom.is_configured():
        return jsonify({"ok": False, "reason": "not_configured",
                        "message": "Classroom未設定（credentials.json なし）。"
                                   "手動入力・写真取り込みで利用できます。"}), 200
    try:
        seen, n = classroom.sync(interactive=False)
        return jsonify({"ok": True, "courses": seen, "upserted": n})
    except classroom.ClassroomUnavailable as e:
        return jsonify({"ok": False, "reason": "unavailable",
                        "message": str(e)}), 200
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "reason": "error",
                        "message": f"{type(e).__name__}: {str(e)[:300]}"}), 200


# --------------------------------------------------------------------------
# Meta — tab badges, last sync, cost meter
# --------------------------------------------------------------------------
@app.route("/api/meta")
def api_meta():
    now = db.now_utc_iso()
    today_n = db.query_one(
        "SELECT COUNT(*) n FROM assignments WHERE status IN ('todo','in_progress') "
        "AND (due_at IS NULL OR due_at >= ?)", (now,))["n"]
    overdue_n = db.query_one(
        "SELECT COUNT(*) n FROM assignments WHERE status IN ('todo','in_progress') "
        "AND due_at IS NOT NULL AND due_at < ?", (now,))["n"]
    proposed_n = db.query_one(
        "SELECT COUNT(*) n FROM assignments WHERE status = 'proposed'")["n"]
    analyzing_n = db.query_one(
        "SELECT COUNT(*) n FROM materials WHERE status IN "
        "('extracting','generating')")["n"]
    review_n = db.query_one(
        "SELECT COUNT(*) n FROM cards WHERE state != 'suspended' AND "
        "(state = 'new' OR (next_due_at IS NOT NULL AND next_due_at <= ?))",
        (now,))["n"]
    last_sync = db.query_one(
        "SELECT MAX(last_synced_at) t FROM assignments WHERE source='classroom'")["t"]
    ok_claude, _ = ai.check_claude()
    settings = db.load_settings()
    import backup
    return jsonify({
        "badges": {"today": today_n, "overdue": overdue_n,
                   "assignments": proposed_n, "review": review_n,
                   "materials": analyzing_n},
        "last_sync": last_sync,
        "claude_ok": ok_claude,
        "classroom_configured": os.path.exists(
            os.path.join(BASE_DIR, "credentials.json")),
        "cost": {"per_call": settings.get("cost_per_call_usd", 0.05),
                 "monthly_budget": settings.get("monthly_call_budget", 200)},
        "backup": backup.status(),   # passive health: {latest, age_days}
    })


# --------------------------------------------------------------------------
# Materials — upload, list, detail, retry/regenerate (feature 2)
# --------------------------------------------------------------------------
@app.route("/uploads/<path:fn>")
def serve_upload(fn):
    return send_from_directory(UPLOADS_DIR, fn)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("files")
    if not files:
        abort(400, "no files")
    out, errors = [], []
    for f in files:
        try:
            out.append(ingest.process_upload(f))
        except ValueError as e:
            errors.append(str(e))
    return jsonify({"materials": out, "errors": errors}), 201


@app.route("/api/materials")
def api_materials():
    rows = db.query("SELECT * FROM materials ORDER BY created_at DESC, id DESC")
    return jsonify([ingest.material_dict(r) for r in rows])


def card_dict(r):
    return {
        "id": r["id"], "course_id": r["course_id"],
        "card_type": r["card_type"], "front": r["front"], "back": r["back"],
        "topic": r["topic"], "origin": r["origin"],
        "confidence": r["confidence"] if "confidence" in r.keys() else None,
        "source_quote": r["source_quote"], "verified": r["verified"],
        "state": r["state"], "next_due_at": r["next_due_at"],
    }


@app.route("/api/materials/<int:mid>")
def api_material_detail(mid):
    m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    if not m:
        abort(404)
    d = ingest.material_dict(m)
    d["extracted_text"] = m["extracted_text"]
    cards = db.query("SELECT * FROM cards WHERE material_id=? ORDER BY origin, id", (mid,))
    d["cards"] = [card_dict(c) for c in cards]
    guides = db.query(
        "SELECT * FROM study_guides WHERE course_id=? ORDER BY id DESC LIMIT 3",
        (m["course_id"],)) if m["course_id"] else []
    d["guides"] = [{"id": g["id"], "scope_desc": g["scope_desc"],
                    "content_md": g["content_md"]} for g in guides]
    return jsonify(d)


@app.route("/api/materials/<int:mid>/retry", methods=["POST"])
def api_material_retry(mid):
    m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    if not m:
        abort(404)
    # User-intentional retry: reset the poison-pill counter so a genuine retry
    # is never blocked by the failed-after-N guard.
    db.write("UPDATE materials SET status='extracting', error_message=NULL, "
             "extracted_json=NULL, extracted_text=NULL, attempts=0 WHERE id=?",
             (mid,))
    worker.enqueue("ingest", {"material_id": mid})
    return jsonify({"ok": True})


@app.route("/api/materials/<int:mid>/regenerate", methods=["POST"])
def api_material_regenerate(mid):
    m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    if not m:
        abort(404)
    # User-intentional regenerate also resets the counter (see /retry).
    db.write("UPDATE materials SET attempts=0 WHERE id=?", (mid,))
    worker.enqueue("generate", {"material_id": mid})
    return jsonify({"ok": True})


@app.route("/api/materials/<int:mid>", methods=["DELETE"])
def api_material_delete(mid):
    m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    if m and m["original_path"]:
        try:
            os.remove(os.path.join(BASE_DIR, m["original_path"]))
        except OSError:
            pass
    db.write("DELETE FROM materials WHERE id=?", (mid,))
    return jsonify({"ok": True})


@app.route("/api/cards")
def api_cards():
    where, params = [], []
    for key in ("course_id", "material_id", "origin"):
        v = request.args.get(key)
        if v:
            where.append(f"{key} = ?")
            params.append(v)
    sql = "SELECT * FROM cards"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    return jsonify([card_dict(r) for r in db.query(sql, params)])


# --------------------------------------------------------------------------
# Review / SRS (feature 3)
# --------------------------------------------------------------------------
@app.route("/api/review/queue")
def api_review_queue():
    return jsonify(srs.get_queue())


@app.route("/api/review/answer", methods=["POST"])
def api_review_answer():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(srs.answer(int(data["card_id"]), data["grade"]))
    except (KeyError, ValueError) as e:
        abort(400, str(e))


@app.route("/api/review/report", methods=["POST"])
def api_review_report():
    data = request.get_json(force=True, silent=True) or {}
    try:
        srs.report_verified(int(data["card_id"]), data["verdict"])
        return jsonify({"ok": True})
    except (KeyError, ValueError) as e:
        abort(400, str(e))


@app.route("/api/review/redistribute", methods=["POST"])
def api_review_redistribute():
    return jsonify({"redistributed": srs.redistribute()})


@app.route("/api/review/weak")
def api_review_weak():
    return jsonify(srs.weak_cards())


@app.route("/api/review/drill")
def api_review_drill():
    return jsonify(srs.drill(request.args.get("course_id"),
                             request.args.get("topic")))


@app.route("/api/review/cram")
def api_review_cram():
    aid = request.args.get("assignment_id")
    try:
        if aid:
            return jsonify(srs.cram_for_assignment(int(aid)))
        exam_date = request.args.get("exam_date")
        if not exam_date:
            abort(400, "assignment_id or exam_date required")
        return jsonify(srs.cram_plan(exam_date, request.args.get("course_id")))
    except ValueError as e:
        abort(400, str(e))


@app.route("/api/mastery")
def api_mastery():
    return jsonify(srs.mastery(request.args.get("course_id")))


@app.route("/studydash.ics")
def api_ics():
    from flask import Response
    return Response(calendar_export.build_ics(), mimetype="text/calendar",
                    headers={"Content-Disposition": "attachment; "
                             "filename=studydash.ics"})


@app.route("/api/exams")
def api_exams():
    now = db.now_utc_iso()
    rows = db.query(
        db.ASSIGN_JOIN + " WHERE a.category='exam' AND a.status IN "
        "('todo','in_progress') AND a.due_at IS NOT NULL AND a.due_at >= ? "
        "ORDER BY a.due_at ASC", (now,))
    return jsonify([assignment_dict(r) for r in rows])


# --------------------------------------------------------------------------
# Sample data (onboarding — deletable). Seeds only when DB is empty.
# --------------------------------------------------------------------------
def seed_sample_data():
    has_any = db.query_one("SELECT COUNT(*) n FROM assignments")["n"]
    has_courses = db.query_one("SELECT COUNT(*) n FROM courses")["n"]
    if has_any or has_courses:
        return
    now = db.now_utc_iso()
    from datetime import datetime, timedelta, timezone
    course_id = db.get_or_create_course("数学")  # -> stem
    due = db.to_utc_iso(datetime.now(timezone.utc) + timedelta(days=1))
    db.write(
        """INSERT INTO assignments
           (course_id, title, description, due_at, category, max_points,
            status, estimated_minutes, source, external_id, updated_at, created_at)
           VALUES (?,?,?,?,?,?,?,?, 'manual', NULL, ?, ?)""",
        (course_id, "演習問題 p.42（サンプル）",
         "これはサンプル課題です。不要なら削除してください。", due,
         "homework", 20, "todo", 60, now, now))
    # 3 sample cards (usable once the review tab lands in Phase 4)
    samples = [
        ("qa", "二次方程式の解の公式は？",
         "x = (-b ± √(b²-4ac)) / 2a", "二次方程式"),
        ("qa", "三角形の内角の和は？", "180°", "図形"),
        ("term", "微分係数の定義は？",
         "f'(a) = lim(h→0) (f(a+h)-f(a))/h", "微分"),
    ]
    for ctype, front, back, topic in samples:
        db.write(
            """INSERT OR IGNORE INTO cards
               (course_id, card_type, front, back, topic, origin,
                content_hash, state, repetitions, current_interval,
                current_ease, created_at)
               VALUES (?,?,?,?,?, 'generated', ?, 'new', 0, 0, 2.5, ?)""",
            (course_id, ctype, front, back, topic,
             db.content_hash(front, back), now))


def bootstrap():
    db.init()
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    seed_sample_data()
    worker.start()
    worker.reconcile()  # re-enqueue materials left mid-analysis by a crash
    import backup
    backup.start_scheduler()  # daily debounced study.db backups (safety net)
    ok, path = ai.check_claude()
    if not ok:
        print(f"[warn] claude CLI not found/executable at {path} — "
              "AI features (photo analysis, generation) will be disabled.")


if __name__ == "__main__":
    bootstrap()
    settings = db.load_settings()
    host = os.environ.get("STUDYDASH_HOST") or settings.get("host", "127.0.0.1")
    port = int(os.environ.get("STUDYDASH_PORT") or settings.get("port", 5000))
    if host not in ("127.0.0.1", "localhost"):
        # public bind -> require a shared token (auto-generate if unset)
        PUBLIC = True
        SHARE_TOKEN = settings.get("share_token") or secrets.token_urlsafe(12)
        print("[studydash] PUBLIC bind — token required.")
        print(f"[studydash] open: http://{host}:{port}/?key={SHARE_TOKEN}")
    else:
        print(f"[studydash] http://{host}:{port}")
    app.run(host=host, port=port, threaded=True, use_reloader=False)
