"""StudyDash Flask app — entry point + routing.

Runs on 127.0.0.1 by default (single-user local dashboard). Times are stored
UTC (D-4) and sent to the browser as raw ISO8601; the browser formats to local.

Concurrency: writes are serialized in db.py (process-wide lock); Flask runs
threaded with the reloader OFF (reloader would double-spawn schedulers/threads).
"""

import json
import os
import secrets
from datetime import datetime

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

import ai
import calendar_export
import db
import generate  # noqa: F401 — registers the 'generate' worker handler at import
import ingest  # noqa: F401 — registers the 'ingest' worker handler at import
import notes
import srs
import support
import version
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
        "SELECT COUNT(*) n FROM cards WHERE state NOT IN ('suspended','proposed') "
        "AND (state = 'new' OR (next_due_at IS NOT NULL AND next_due_at <= ?))",
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
        "content_lang": db.content_lang(),   # output language for generated study content
        "review_new_cap": int(settings.get("review_new_cap", 10)),   # L: pacing display
        "scheduler": srs.scheduler_name(),   # O: SM-2 (default) or FSRS
        "version": version.__version__,   # M: shown in the footer, asked for in support
        "backup": backup.status(),   # passive health: {latest, age_days}
    })


@app.route("/api/feedback", methods=["GET", "POST"])
def api_feedback():
    """Phase N: problem reports. POST builds a shareable markdown report and saves
    it under feedback/; GET lists what this copy has already reported. Nothing is
    transmitted anywhere — the person sends the file to the maintainer themselves."""
    if request.method == "GET":
        rows = db.query(
            "SELECT id, kind, message, file_path, created_at FROM feedback_reports "
            "ORDER BY id DESC LIMIT 50")
        return jsonify({"reports": [dict(r) for r in rows],
                        "kinds": support.KINDS})
    data = request.get_json(silent=True) or {}
    message = db.text_cell(data.get("message")).strip()
    if not message:
        return jsonify({"ok": False,
                        "error": "何が起きたかを書いてください。"}), 200
    try:
        saved = support.save_report(
            db.text_cell(data.get("kind")) or "other",
            message,
            include_error=bool(data.get("include_error")),
            include_log=bool(data.get("include_log")))
    except Exception as e:  # noqa: BLE001 — reporting a problem must never itself 500
        return jsonify({"ok": False, "error": "レポートを保存できませんでした: %s" % e}), 200
    return jsonify({"ok": True, **saved})


@app.route("/api/settings", methods=["GET", "PATCH"])
def api_settings():
    """Editable app settings. Currently just content_lang (auto|ja|en) — the output
    language for AI-generated study content (cards/quiz/summary)."""
    def _state():
        return {"content_lang": db.content_lang(),
                "scheduler": srs.scheduler_name(),
                "fsrs_retention": srs.target_retention()}

    if request.method == "GET":
        return jsonify(_state())
    data = request.get_json(silent=True) or {}
    patch = {}
    if "content_lang" in data:
        v = (data.get("content_lang") or "").strip()
        if v not in db.VALID_CONTENT_LANGS:
            abort(400, "invalid content_lang")
        patch["content_lang"] = v
    if "scheduler" in data:
        v = db.text_cell(data.get("scheduler")).strip().lower()
        if v not in srs.VALID_SCHEDULERS:
            abort(400, "invalid scheduler")
        patch["scheduler"] = v
    if "fsrs_retention" in data:
        try:
            r = float(data.get("fsrs_retention"))
        except (TypeError, ValueError):
            abort(400, "invalid fsrs_retention")
        if not 0.70 <= r <= 0.97:
            abort(400, "fsrs_retention must be between 0.70 and 0.97")
        patch["fsrs_retention"] = r
    if not patch:
        abort(400, "nothing to update")
    db.save_settings(patch)
    return jsonify(_state())


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
        "source_loc": (json.loads(r["source_loc"])
                       if ("source_loc" in r.keys() and r["source_loc"]) else None),
        # H0: per-modality render structure (excluded from D-5 content_hash)
        "media_json": (json.loads(r["media_json"])
                       if ("media_json" in r.keys() and r["media_json"]) else None),
        # J: cached ja/en translations for language-flip (also excluded from the hash)
        "translation": (json.loads(r["translation_json"])
                        if ("translation_json" in r.keys() and r["translation_json"]) else None),
        "state": r["state"], "next_due_at": r["next_due_at"],
    }


def quiz_dict(r):
    # Phase I: a material comprehension quiz. questions_json = the whole set.
    return {
        "id": r["id"], "material_id": r["material_id"], "course_id": r["course_id"],
        "format": r["format"], "scope_desc": r["scope_desc"],
        "questions": json.loads(r["questions_json"]) if r["questions_json"] else [],
        "created_at": r["created_at"],
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
    # Course-level 要点まとめ only. Phase I material summaries carry BOTH course_id
    # (for ON DELETE CASCADE cleanup) and material_id, so `material_id IS NULL`
    # keeps them out of this course list — they surface via summary_guide instead
    # (else a material's summary would leak onto sibling materials and duplicate).
    guides = db.query(
        "SELECT * FROM study_guides WHERE course_id=? AND material_id IS NULL "
        "ORDER BY id DESC LIMIT 3",
        (m["course_id"],)) if m["course_id"] else []
    d["guides"] = [{"id": g["id"], "scope_desc": g["scope_desc"],
                    "content_md": g["content_md"]} for g in guides]
    # Phase I study modes: latest material-scoped まとめ + latest quiz (if any).
    sg = db.query_one(
        "SELECT * FROM study_guides WHERE material_id=? ORDER BY id DESC LIMIT 1", (mid,))
    d["summary_guide"] = ({"id": sg["id"], "scope_desc": sg["scope_desc"],
                           "content_md": sg["content_md"]} if sg else None)
    qz = db.query_one(
        "SELECT * FROM quizzes WHERE material_id=? ORDER BY id DESC LIMIT 1", (mid,))
    d["quiz"] = quiz_dict(qz) if qz else None
    return jsonify(d)


@app.route("/api/materials/<int:mid>/card", methods=["POST"])
def api_material_add_card(mid):
    """Phase E4: create a review card sourced from this material, optionally with
    a precise source location. Additive over the extract/generate pipeline; the
    card flows into the SAME queue (state='new') and reuses content_hash dedup
    (D-5). source_loc is a free JSON blob describing where in the file it came
    from (e.g. {"quote","char_start","char_end"} or {"region":[x,y,w,h]})."""
    m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    if not m:
        abort(404)
    data = request.get_json(silent=True) or {}
    front = (data.get("front") or "").strip()
    back = (data.get("back") or "").strip()
    if not front or not back:
        abort(400, "front and back are required")
    source_quote = (data.get("source_quote") or "").strip() or None
    loc = data.get("source_loc")
    loc_json = json.dumps(loc, ensure_ascii=False) if loc else None
    card_type = (data.get("card_type") or "qa").strip() or "qa"
    topic = (data.get("topic") or "").strip() or None
    ch = db.content_hash(front, back)
    db.write(
        "INSERT OR IGNORE INTO cards (course_id, material_id, card_type, front, back, "
        "topic, origin, source_quote, source_loc, content_hash, state, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (m["course_id"], mid, card_type, front, back, topic, "extracted",
         source_quote, loc_json, ch, "new", db.now_utc_iso()))
    card = db.query_one(
        "SELECT * FROM cards WHERE course_id IS ? AND content_hash=?",
        (m["course_id"], ch))
    return jsonify(card_dict(card)), 201


@app.route("/api/materials/<int:mid>/approve", methods=["POST"])
def api_material_approve(mid):
    """Phase G1: promote PROPOSED cards into the review queue. The upload
    pipeline drafts cards as state='proposed' (never auto-queued) so nothing is
    studied until the user confirms the plan here. Selected cards -> 'new'
    (enter the queue); the rest of this material's proposed cards -> 'suspended'
    (dropped from the plan but recoverable). Omit card_ids to approve ALL
    (the 推奨で学ぶ one-click path)."""
    m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    if not m:
        abort(404)
    data = request.get_json(silent=True) or {}
    ids = data.get("card_ids")
    proposed = {r["id"] for r in db.query(
        "SELECT id FROM cards WHERE material_id=? AND state='proposed'", (mid,))}
    approve = proposed if ids is None else {int(i) for i in ids} & proposed
    drop = proposed - approve
    stmts = [("UPDATE cards SET state='new' WHERE id=?", (cid,)) for cid in approve]
    stmts += [("UPDATE cards SET state='suspended' WHERE id=?", (cid,)) for cid in drop]
    if stmts:
        db.write_many(stmts)
    return jsonify({"ok": True, "approved": len(approve), "dropped": len(drop)})


# --------------------------------------------------------------------------
# Study methods (Phase G2) — the "学び方" catalog + AI recast of a card into one
# --------------------------------------------------------------------------
@app.route("/api/methods")
def api_methods():
    import methods
    return jsonify(methods.all_methods())


@app.route("/api/methods", methods=["POST"])
def api_methods_create():
    import methods
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        abort(400, "name required")
    base = (data.get("base") or "qa").strip()
    instruction = (data.get("instruction") or "").strip() or None
    mid = db.write(
        "INSERT INTO study_methods (name, base, instruction, created_at) "
        "VALUES (?,?,?,?)", (name, base, instruction, db.now_utc_iso()))
    return jsonify(methods.custom_dict(
        db.query_one("SELECT * FROM study_methods WHERE id=?", (mid,)))), 201


@app.route("/api/methods/<int:cid>", methods=["DELETE"])
def api_methods_delete(cid):
    db.write("DELETE FROM study_methods WHERE id=?", (cid,))
    return jsonify({"ok": True})


@app.route("/api/cards/<int:cid>/recast", methods=["POST"])
def api_card_recast(cid):
    """Recast a card into the chosen method (one AI call). Synchronous: this is a
    user-initiated, single-card transform, so we block briefly and return the new
    card rather than routing through the background worker."""
    import methods
    data = request.get_json(silent=True) or {}
    method = methods.get_method((data.get("method") or "").strip())
    if not method:
        abort(400, "unknown method")
    if not db.query_one("SELECT id FROM cards WHERE id=?", (cid,)):
        abort(404)
    try:
        new = generate.recast_card(cid, method)
    except generate.ai.ClaudeError as e:
        return jsonify({"ok": False, "message": f"AI呼び出し失敗: {str(e)[:200]}"}), 200
    except ValueError as e:
        return jsonify({"ok": False, "message": f"変換に失敗: {str(e)[:200]}"}), 200
    return jsonify({"ok": True, "card": card_dict(new)})


@app.route("/api/cards/<int:cid>/translate", methods=["POST"])
def api_card_translate(cid):
    """Phase J: translate a card's front/back into 'ja' or 'en' for language-flip
    review. Cached per-lang in cards.translation_json (excluded from the D-5 hash),
    so a re-flip is instant and the SRS card is never duplicated. One AI call, sync."""
    if not db.query_one("SELECT id FROM cards WHERE id=?", (cid,)):
        abort(404)
    data = request.get_json(silent=True) or {}
    lang = (data.get("lang") or "").strip()
    try:
        res = generate.translate_card(cid, lang)
    except generate.ai.ClaudeError as e:
        return jsonify({"ok": False, "message": f"AI呼び出し失敗: {str(e)[:200]}"}), 200
    except ValueError as e:
        return jsonify({"ok": False, "message": f"翻訳に失敗: {str(e)[:200]}"}), 200
    if not res:
        abort(404)
    return jsonify({"ok": True, "translation": res})


# --------------------------------------------------------------------------
# Phase I — material study modes: quiz + summary (in addition to flashcards).
# Both are synchronous single AI calls (like recast); AI/parse failures return
# ok:false with a message (HTTP 200) so the UI can show it inline.
# --------------------------------------------------------------------------
@app.route("/api/materials/<int:mid>/quiz", methods=["GET", "POST"])
def api_material_quiz(mid):
    if not db.query_one("SELECT id FROM materials WHERE id=?", (mid,)):
        abort(404)
    if request.method == "GET":
        qz = db.query_one(
            "SELECT * FROM quizzes WHERE material_id=? ORDER BY id DESC LIMIT 1", (mid,))
        return jsonify({"quiz": quiz_dict(qz) if qz else None})
    data = request.get_json(silent=True) or {}
    fmt = "mixed" if data.get("format") == "mixed" else "written"
    try:
        count = int(data["count"]) if data.get("count") is not None else None
    except (TypeError, ValueError):
        count = None
    scope = (data.get("scope") or "").strip() or None
    lang = (data.get("lang") or "").strip() or None   # None -> saved setting (auto)
    try:
        qz = generate.generate_quiz(mid, fmt, count, scope, lang)
    except generate.ai.ClaudeError as e:
        return jsonify({"ok": False, "message": f"AI呼び出し失敗: {str(e)[:200]}"}), 200
    except ValueError as e:
        return jsonify({"ok": False, "message": f"生成に失敗: {str(e)[:200]}"}), 200
    if not qz:
        abort(404)
    return jsonify({"ok": True, "quiz": quiz_dict(qz)})


@app.route("/api/materials/<int:mid>/glossary", methods=["GET", "POST"])
def api_material_glossary(mid):
    """K: read / rebuild / hand-edit a material's canonical term table.

    POST {"terms": [...]} saves the user's own table — an edit is authoritative and
    is NOT overwritten by a later build. POST {"rebuild": true} re-derives it from
    the material (one AI call). The saved table then binds every later card, quiz,
    summary and translation for this material."""
    if not db.query_one("SELECT id FROM materials WHERE id=?", (mid,)):
        abort(404)
    if request.method == "GET":
        m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
        return jsonify({"glossary": ingest._glossary_terms(m)})
    data = request.get_json(silent=True) or {}
    if data.get("rebuild"):
        try:
            terms = generate.build_glossary(mid, force=True)
        except generate.ai.ClaudeError as e:
            return jsonify({"ok": False, "message": f"AI呼び出し失敗: {str(e)[:200]}"}), 200
        except ValueError as e:
            return jsonify({"ok": False, "message": f"用語表を作れませんでした: {str(e)[:200]}"}), 200
        return jsonify({"ok": True, "glossary": terms})
    raw = data.get("terms")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "message": "terms が不正です"}), 200
    terms = generate.clean_glossary_terms(raw)
    db.write("UPDATE materials SET glossary_json=? WHERE id=?",
             (json.dumps({"terms": terms}, ensure_ascii=False), mid))
    return jsonify({"ok": True, "glossary": terms})


@app.route("/api/materials/<int:mid>/summary", methods=["POST"])
def api_material_summary(mid):
    if not db.query_one("SELECT id FROM materials WHERE id=?", (mid,)):
        abort(404)
    data = request.get_json(silent=True) or {}
    scope = (data.get("scope") or "").strip() or None
    lang = (data.get("lang") or "").strip() or None   # None -> saved setting (auto)
    try:
        g = generate.generate_summary(mid, scope, lang)
    except generate.ai.ClaudeError as e:
        return jsonify({"ok": False, "message": f"AI呼び出し失敗: {str(e)[:200]}"}), 200
    except ValueError as e:
        return jsonify({"ok": False, "message": f"生成に失敗: {str(e)[:200]}"}), 200
    if not g:
        abort(404)
    return jsonify({"ok": True, "summary": {
        "id": g["id"], "scope_desc": g["scope_desc"], "content_md": g["content_md"]}})


@app.route("/api/materials/<int:mid>/draft", methods=["POST"])
def api_material_draft(mid):
    """Phase H3: auto-generate review cards from this material with an optional
    free-text instruction + range. Cards enter as PROPOSED (G1 gate) so nothing is
    studied until the user confirms. Synchronous single AI call."""
    if not db.query_one("SELECT id FROM materials WHERE id=?", (mid,)):
        abort(404)
    data = request.get_json(silent=True) or {}
    instruction = (data.get("instruction") or "").strip() or None
    scope = (data.get("scope") or "").strip() or None
    lang = (data.get("lang") or "").strip() or None   # None -> saved setting (auto)
    try:
        res = generate.generate_draft(mid, instruction, scope, lang)
    except generate.ai.ClaudeError as e:
        return jsonify({"ok": False, "message": f"AI呼び出し失敗: {str(e)[:200]}"}), 200
    except ValueError as e:
        return jsonify({"ok": False, "message": f"生成に失敗: {str(e)[:200]}"}), 200
    if not res:
        abort(404)
    return jsonify({"ok": True, **res})


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
    """Delete a material. Its cards are KEPT by default and simply detached
    (cards.material_id is ON DELETE SET NULL), so review history is never lost by
    tidying up the materials list. ?cards=1 deletes that material's cards too —
    the caller has to ask for it explicitly, and is told the count first."""
    m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    if not m:
        return jsonify({"ok": True, "deleted_cards": 0})
    drop_cards = str(request.args.get("cards", "")).strip() in ("1", "true", "yes")
    deleted_cards = 0
    if drop_cards:
        deleted_cards = db.query_one(
            "SELECT COUNT(*) n FROM cards WHERE material_id=?", (mid,))["n"]
        db.write("DELETE FROM cards WHERE material_id=?", (mid,))
    # Occlusion cards render FROM the image file — if any survive this delete
    # (the keep-cards default detaches them), removing the file would leave them
    # permanently showing 画像を読み込めません. The file only goes when its last
    # dependent card does.
    keep_file = (not drop_cards) and db.query_one(
        "SELECT COUNT(*) n FROM cards WHERE material_id=? AND card_type='occlusion'",
        (mid,))["n"] > 0
    if m["original_path"] and not keep_file:
        try:
            os.remove(os.path.join(BASE_DIR, m["original_path"]))
        except OSError:
            pass
    db.write("DELETE FROM materials WHERE id=?", (mid,))
    return jsonify({"ok": True, "deleted_cards": deleted_cards})


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
    q = srs.get_queue()
    # G3: strategies only reorder / re-ask. Additive query param, additive response
    # key — the existing contract ({cards,new_count,due_count}) is untouched.
    strategies = srs.clean_strategies(request.args.getlist("strategy") or
                                      request.args.get("strategies"))
    if "interleave" in strategies:
        q["cards"] = srs.interleave(q["cards"])
    q["strategies"] = strategies
    return jsonify(q)


@app.route("/api/materials/<int:mid>/target", methods=["POST"])
def api_material_target(mid):
    """L: set / clear a material's study deadline (calendar date). The daily
    review queue then paces this material's unseen cards to finish by it."""
    if not db.query_one("SELECT id FROM materials WHERE id=?", (mid,)):
        abort(404)
    raw = (request.get_json(silent=True) or {}).get("date")
    date = db.text_cell(raw)
    if date:
        try:
            y, m, d = [int(x) for x in date.split("-")]
            date = "%04d-%02d-%02d" % (y, m, d)
            datetime(y, m, d)                      # reject 2026-13-99
        except (ValueError, TypeError):
            return jsonify({"ok": False, "message": "日付は YYYY-MM-DD で指定してください"}), 200
    db.write("UPDATE materials SET target_date=? WHERE id=?", (date or None, mid))
    pacing = [p for p in srs.paced_materials() if p["material_id"] == mid]
    return jsonify({"ok": True, "target_date": date or None,
                    "pacing": pacing[0] if pacing else None})


@app.route("/api/materials/<int:mid>/occlusion", methods=["POST"])
def api_material_occlusion(mid):
    """H6: create one image-occlusion card per drawn region. No AI call."""
    if not db.query_one("SELECT id FROM materials WHERE id=?", (mid,)):
        abort(404)
    data = request.get_json(silent=True) or {}
    rects = data.get("rects")
    if not isinstance(rects, list) or not rects:
        return jsonify({"ok": False, "message": "範囲が指定されていません"}), 200
    try:
        res = generate.create_occlusion_cards(mid, rects, data.get("prompt"))
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)[:200]}), 200
    if res is None:
        abort(404)
    return jsonify({"ok": True, "created": [card_dict(c) for c in res["created"]],
                    "duplicates": res["duplicates"]})


@app.route("/api/cards/<int:cid>/reverse", methods=["POST"])
def api_card_reverse(cid):
    """H5 双方向: create the front/back mirror of a card. No AI call — the swap is
    mechanical, and the swapped text hashes differently, so the mirror is its own
    card with its own SRS state. The original is left untouched."""
    if not db.query_one("SELECT id FROM cards WHERE id=?", (cid,)):
        abort(404)
    try:
        new = generate.reverse_card(cid)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)[:200]}), 200
    if not new:
        abort(404)
    return jsonify({"ok": True, "card": card_dict(new)})


@app.route("/api/cards/<int:cid>/distractors")
def api_card_distractors(cid):
    """G3 recognition mode: wrong options drawn from other cards in the course."""
    if not db.query_one("SELECT id FROM cards WHERE id=?", (cid,)):
        abort(404)
    try:
        limit = min(6, max(1, int(request.args.get("limit", 3))))
    except (TypeError, ValueError):
        limit = 3
    return jsonify({"distractors": srs.distractors_for(cid, limit)})


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


@app.route("/api/review/materials")
def api_review_materials():
    """Phase J: study-ready cards grouped by their source material (+ counts), for
    the review-home 'study by material' picker."""
    return jsonify(srs.review_materials())


@app.route("/api/review/by-material")
def api_review_by_material():
    """Phase J: a review queue from one or more materials (merge). Repeat
    material_id= for several; material_id=none selects material-less cards.
    G3: ?strategy=interleave round-robins the merged materials."""
    cards = srs.by_materials(request.args.getlist("material_id"))
    if "interleave" in srs.clean_strategies(request.args.getlist("strategy")):
        cards = srs.interleave(cards)
    return jsonify(cards)


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
    course_id = db.get_or_create_course("StudyDash の使い方")
    due = db.to_utc_iso(datetime.now(timezone.utc) + timedelta(days=1))
    db.write(
        """INSERT INTO assignments
           (course_id, title, description, due_at, category, max_points,
            status, estimated_minutes, source, external_id, updated_at, created_at)
           VALUES (?,?,?,?,?,?,?,?, 'manual', NULL, ?, ?)""",
        (course_id, "はじめての教材をアップロードしてみる",
         "「教材」タブから写真か PDF を1つ入れてみてください。"
         "AI が中身を読み取り、カード候補を提案します（確認してから追加されます）。"
         "これはサンプルです。終わったら削除してかまいません。", due,
         "homework", 0, "todo", 15, now, now))
    # The demo deck IS the tutorial: reviewing it once teaches every card format the
    # app can render, so a new user meets the features instead of reading about them.
    samples = [
        ("qa", "StudyDash で教材からカードを作るには？",
         "「教材」タブで写真か PDF をアップロードすると、AI が読み取ってカードを提案します。"
         "提案は**確認してから**キューに入ります。", "使い方", None),
        ("term", "間隔反復（かんかくはんぷく）とは？",
         "忘れる直前に復習する方法。正解すると次に出るまでの間隔が伸び、"
         "間違えると短くなります。", "使い方", None),
        ("cloze", "教材に試験日を設定すると、1日の新規カード枚数は ___ から自動で決まります。",
         "残り日数", "使い方", None),
        ("choice", "復習中に「🌐」ボタンを押すと何が起きる？",
         "そのカードだけ日本語と英語が入れ替わる",
         "使い方",
         {"choices": ["カードが削除される", "次のカードに進む",
                      "デッキ全体の言語が変わる"]}),
        ("steps", "写真をアップロードしてからカードが増えるまでの流れは？",
         "アップロード → AI が全文を書き起こす → カード候補を提案 → あなたが承認 → 復習キューに追加",
         "使い方",
         {"steps": ["「教材」タブで写真や PDF をアップロード",
                    "AI が全文を書き起こす（少し時間がかかります）",
                    "AI がカード候補を提案する",
                    "内容を確認して「承認」する",
                    "承認したカードだけが復習キューに入る"]}),
        ("explain", "このアプリを誰かに一言で説明すると？",
         "教材を取り込むと AI がカードを作り、忘れる直前に出題してくれる、"
         "自分のパソコンの中だけで動く学習アプリ。", "使い方",
         {"rubric": "「自分のパソコンだけで動く」に触れる・"
                    "「AI がカードを作る」に触れる・「忘れる直前に復習」に触れる"}),
    ]
    for ctype, front, back, topic, media in samples:
        db.write(
            """INSERT OR IGNORE INTO cards
               (course_id, card_type, front, back, topic, origin, media_json,
                content_hash, state, repetitions, current_interval,
                current_ease, created_at)
               VALUES (?,?,?,?,?, 'generated', ?,?, 'new', 0, 0, 2.5, ?)""",
            (course_id, ctype, front, back, topic,
             json.dumps(media, ensure_ascii=False) if media else None,
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
