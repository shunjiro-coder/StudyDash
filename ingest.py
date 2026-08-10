"""Ingest pipeline: dropped file -> material row -> claude extraction ->
proposed assignments + extracted cards -> hand off to generation.

Registers the 'ingest' worker handler at import time.
"""

import json
import os
import uuid
from datetime import datetime

import ai
import db
import worker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
EXTRACT_PROMPT = os.path.join(BASE_DIR, "prompts", "extract.txt")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
HEIC_EXTS = {".heic", ".heif"}
RAW_PREVIEW = 300  # chars of raw output kept on failure
FAIL_EXHAUSTED_MSG = (
    "何度か試しましたが解析できませんでした。お手数ですが手動で入力してください。")


def _classify(ext):
    if ext in HEIC_EXTS or ext in IMAGE_EXTS:
        return "photo"
    if ext == ".pdf":
        return "pdf"
    return None


def _max_upload_bytes():
    return int(db.load_settings().get("max_upload_mb", 25)) * 1024 * 1024


def _stream_size(storage):
    """Byte size of an uploaded file without reading it into memory."""
    stream = storage.stream
    pos = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(pos)
    return size


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _sips(*args):
    import subprocess
    subprocess.run(["sips", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _postprocess(raw_path, ext, kind, base):
    """HEIC->JPEG, then resize photos. PDFs pass through untouched (small text
    would degrade). Returns the final absolute path."""
    path = raw_path
    if ext in HEIC_EXTS:
        jpg = os.path.join(UPLOADS_DIR, base + ".jpg")
        try:
            _sips("-s", "format", "jpeg", raw_path, "--out", jpg)
        except Exception:  # noqa: BLE001 — surface as a clean upload error
            _safe_remove(jpg)   # remove any partial output; caller removes raw
            raise
        _safe_remove(raw_path)
        path = jpg
    if kind == "photo":
        px = int(db.load_settings().get("image_max_px", 1500))
        try:
            _sips("-Z", str(px), path)
        except Exception:  # noqa: BLE001 — resize failure shouldn't block ingest
            pass
    return path


def process_upload(storage):
    """Save an uploaded file, normalize it, create the material, enqueue ingest.
    Returns the material dict for optimistic UI (server didn't wait for AI)."""
    filename = storage.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    kind = _classify(ext)
    if kind is None:
        raise ValueError(
            f"未対応の形式です（{ext or '不明'}）。写真（JPG/PNG/HEIC）か PDF にしてください。")
    # Per-file size cap -> errors[] (MAX_CONTENT_LENGTH would 413 the WHOLE
    # batch, so a single oversized photo would silently sink the others).
    limit = _max_upload_bytes()
    if _stream_size(storage) > limit:
        raise ValueError(
            f"この写真は大きすぎます（{limit // (1024 * 1024)}MB超）: {filename}")
    base = uuid.uuid4().hex
    raw_path = os.path.join(UPLOADS_DIR, base + ext)
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    storage.save(raw_path)
    try:
        final_path = _postprocess(raw_path, ext, kind, base)
    except Exception as e:  # noqa: BLE001 — e.g. HEIC->JPEG conversion failed
        _safe_remove(raw_path)  # no orphan left behind
        raise ValueError(f"画像の変換に失敗しました: {filename}") from e
    rel = os.path.relpath(final_path, BASE_DIR)  # e.g. uploads/xxxx.jpg
    mid = db.write(
        "INSERT INTO materials (kind, original_path, status, created_at) "
        "VALUES (?, ?, 'extracting', ?)",
        (kind, rel, db.now_utc_iso()))
    worker.enqueue("ingest", {"material_id": mid})
    return material_dict(db.query_one("SELECT * FROM materials WHERE id=?", (mid,)))


def material_dict(r):
    ej = None
    if r["extracted_json"]:
        try:
            ej = json.loads(r["extracted_json"])
        except (TypeError, ValueError):
            ej = None
    keys = r.keys()
    attempts = (r["attempts"] if "attempts" in keys else 0) or 0
    card_count = db.query_one(
        "SELECT COUNT(*) n FROM cards WHERE material_id=?", (r["id"],))["n"]
    # G1: cards drafted but not yet approved into the review queue
    proposed_count = db.query_one(
        "SELECT COUNT(*) n FROM cards WHERE material_id=? AND state='proposed'",
        (r["id"],))["n"]
    return {
        "id": r["id"], "kind": r["kind"], "status": r["status"],
        "course_id": r["course_id"], "assignment_id": r["assignment_id"],
        "original_path": r["original_path"],
        "thumb_url": "/" + r["original_path"] if r["original_path"] else None,
        "summary": (ej or {}).get("summary") if ej else None,
        "error_message": r["error_message"],
        "has_text": bool(r["extracted_text"]),
        # additive fields for the honest-completion + failed-after-N UI (E):
        "card_count": card_count,
        "proposed_count": proposed_count,
        "attempts": attempts,
        "attempts_exhausted": bool(attempts >= db.max_material_attempts()),
        # K: the material's canonical term table, once one has been built
        "glossary": _glossary_terms(r),
        "created_at": r["created_at"],
    }


def _glossary_terms(r):
    """Phase K: expose materials.glossary_json as a list (never None) so the UI can
    show the term table. Tolerant of an older row that predates the column."""
    if "glossary_json" not in r.keys() or not r["glossary_json"]:
        return []
    try:
        obj = json.loads(r["glossary_json"])
    except (TypeError, ValueError):
        return []
    terms = obj.get("terms") if isinstance(obj, dict) else obj
    return terms if isinstance(terms, list) else []


def _due_from_date(s):
    if not s:
        return None
    try:
        y, m, d = [int(x) for x in str(s)[:10].split("-")]
        local = datetime(y, m, d, 23, 59, 0).astimezone()  # local -> UTC
        return db.to_utc_iso(local)
    except (ValueError, TypeError):
        return None


def _insert_extracted_cards(course_id, material_id, cards, extracted_text=""):
    if not course_id or not cards:
        return 0
    now = db.now_utc_iso()
    n = 0
    for c in cards:
        front = db.text_cell(c.get("front"))
        back = db.text_cell(c.get("back"))
        if not front or not back:
            continue
        # E5: locate the verbatim quote in the transcription (offsets or null).
        sq = db.text_cell(c.get("source_quote")) or None
        loc_json = (json.dumps(db.locate(sq, extracted_text), ensure_ascii=False)
                    if sq else None)
        db.write(
            """INSERT OR IGNORE INTO cards
               (course_id, material_id, card_type, front, back, topic, origin,
                source_quote, source_loc, content_hash, state, repetitions,
                current_interval, current_ease, created_at)
               VALUES (?,?,?,?,?,?, 'extracted', ?, ?, ?, 'proposed', 0, 0, 2.5, ?)""",
            (course_id, material_id, c.get("card_type") or "qa", front, back,
             c.get("topic"), sq, loc_json,
             db.content_hash(front, back), now))
        n += 1
    return n


def _insert_proposed(course_id, material_id, items):
    if not items:
        return 0
    now = db.now_utc_iso()
    n = 0
    VALID = {"homework", "quiz", "exam", "project", "other"}
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        cat = it.get("category") if it.get("category") in VALID else "homework"
        db.write(
            """INSERT INTO assignments
               (course_id, title, due_at, category, max_points, status,
                source, external_id, updated_at, created_at)
               VALUES (?, ?, ?, ?, ?, 'proposed', 'photo', NULL, ?, ?)""",
            (course_id, title, _due_from_date(it.get("due")), cat,
             it.get("points"), now, now))
        n += 1
    return n


def _extract_with_retry(prompt, model, add_dirs, source_path=None):
    """Run claude, parse JSON, one retry on parse failure. Returns (obj, raw).

    The timeout scales with the source file — transcribing a long PDF takes far
    longer than reading one slide, and a flat budget just kills it.
    """
    last_raw = ""
    timeout = ai.timeout_for(source_path)
    for attempt in range(2):
        data = ai.run_claude(prompt, model=model, add_dirs=add_dirs,
                             allowed_tools="Read", cwd=BASE_DIR, timeout=timeout)
        last_raw = ai.result_text(data)
        try:
            return ai.extract_json(last_raw), last_raw
        except ValueError:
            continue
    raise ValueError(last_raw[:RAW_PREVIEW] or "empty")


def ingest_handler(payload):
    mid = payload["material_id"]
    m = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    if not m:
        return

    # poison-pill guard: after N total claude tries, stop re-spawning. A hard
    # crash mid-call + reconcile/KeepAlive would otherwise re-bill claude
    # forever. User /retry resets the counter (see app.py).
    if db.material_attempts(mid) >= db.max_material_attempts():
        db.write("UPDATE materials SET status='failed', error_message=? "
                 "WHERE id=?", (FAIL_EXHAUSTED_MSG, mid))
        return

    # re-analysis cache: skip the claude call if we already have extraction
    if m["extracted_json"]:
        db.write("UPDATE materials SET status='generating' WHERE id=?", (mid,))
        worker.enqueue("generate", {"material_id": mid})
        return

    db.write("UPDATE materials SET status='extracting', error_message=NULL "
             "WHERE id=?", (mid,))
    path = m["original_path"]  # relative to cwd=BASE_DIR
    with open(EXTRACT_PROMPT, encoding="utf-8") as f:
        prompt = f.read().format(path=path)
    model = db.load_settings().get("model_image")
    db.bump_material_attempts(mid)  # count BEFORE the call (crash-safe)
    try:
        obj, _ = _extract_with_retry(prompt, model, add_dirs=[UPLOADS_DIR],
                                     source_path=os.path.join(BASE_DIR, path))
    except ai.ClaudeError as e:
        db.write("UPDATE materials SET status='failed', error_message=? WHERE id=?",
                 (f"AI呼び出し失敗: {str(e)[:RAW_PREVIEW]}", mid))
        return
    except ValueError as e:
        db.write("UPDATE materials SET status='failed', error_message=? WHERE id=?",
                 (f"解析結果を読めませんでした: {str(e)[:RAW_PREVIEW]}", mid))
        return

    # associate a course from the inferred subject (drop-photo -> cards)
    course_hint = (obj.get("course_hint") or "").strip()
    subject = obj.get("subject_type")
    if subject not in ("stem", "memo", "lang", "other"):
        subject = None
    course_id = m["course_id"]
    if not course_id and course_hint:
        course_id = db.get_or_create_course(course_hint, subject_type=subject)

    db.write(
        "UPDATE materials SET extracted_text=?, extracted_json=?, course_id=?, "
        "status='generating' WHERE id=?",
        (obj.get("text"), json.dumps(obj, ensure_ascii=False), course_id, mid))

    # Persisting is wrapped: a malformed payload here must never leave the
    # material stuck in 'generating', because worker._run swallows the traceback
    # and reconcile() re-enqueues every 'generating' row on the next boot — so it
    # would re-crash forever with no way for the user to see or retry it.
    try:
        _insert_extracted_cards(course_id, mid, obj.get("extracted_cards") or [],
                                obj.get("text") or "")
        _insert_proposed(course_id, mid, obj.get("proposed_assignments") or [])
    except Exception as e:
        db.write("UPDATE materials SET status='failed', error_message=? WHERE id=?",
                 (f"カード保存に失敗しました: {str(e)[:RAW_PREVIEW]}", mid))
        return

    worker.enqueue("generate", {"material_id": mid})


worker.register("ingest", ingest_handler)
