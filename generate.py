"""Review-material generation (feature 2).

Branches by course subject_type:
  stem -> solution steps + check points (NEVER final numeric answers)
  memo -> term cards + causal links + Q&A
  lang -> vocab/kanji + grammar + reading
  other -> tracker only (no generation)

Regeneration uses INSERT OR IGNORE on UNIQUE(course_id, content_hash), so a
re-run does NOT reset the SRS state / `verified` of existing cards (that would
throw away review history).

Registers the 'generate' worker handler at import time.
"""

import json
import os

import ai
import db
import worker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS = {
    "stem": os.path.join(BASE_DIR, "prompts", "stem.txt"),
    "memo": os.path.join(BASE_DIR, "prompts", "memo.txt"),
    "lang": os.path.join(BASE_DIR, "prompts", "lang.txt"),
}
RAW_PREVIEW = 300
FAIL_EXHAUSTED_MSG = (
    "何度か試しましたが解析できませんでした。お手数ですが手動で入力してください。")


def _generate_with_retry(prompt, model):
    last_raw = ""
    for _ in range(2):
        data = ai.run_claude(prompt, model=model, allowed_tools="Read",
                             cwd=BASE_DIR)
        last_raw = ai.result_text(data)
        try:
            return ai.extract_json(last_raw)
        except ValueError:
            continue
    raise ValueError(last_raw[:RAW_PREVIEW] or "empty")


def _insert_generated_cards(course_id, material_id, cards, extracted_text=""):
    now = db.now_utc_iso()
    added = 0
    for c in cards:
        front = (c.get("front") or "").strip()
        back = (c.get("back") or "").strip()
        if not front or not back:
            continue
        conf = c.get("confidence") if c.get("confidence") in ("high", "low") else None
        # E5: a verbatim quote lets the viewer show WHERE this card came from.
        sq = (c.get("source_quote") or "").strip() or None
        loc_json = (json.dumps(db.locate(sq, extracted_text), ensure_ascii=False)
                    if sq else None)
        ch = db.content_hash(front, back)
        cur = db.write_returning(
            """INSERT OR IGNORE INTO cards
               (course_id, material_id, card_type, front, back, topic, origin,
                confidence, source_quote, source_loc, content_hash, state,
                repetitions, current_interval, current_ease, created_at)
               VALUES (?,?,?,?,?,?, 'generated', ?,?,?,?, 'new', 0, 0, 2.5, ?)""",
            (course_id, material_id, c.get("card_type") or "qa", front, back,
             c.get("topic"), conf, sq, loc_json, ch, now))
        if cur.rowcount:
            added += 1
        elif sq:
            # E5 backfill: card already exists (D-5 dedup). Refresh ONLY its
            # source location — never front/back/SRS state (keeps review history).
            db.write("UPDATE cards SET source_quote=?, source_loc=? "
                     "WHERE course_id IS ? AND content_hash=?",
                     (sq, loc_json, course_id, ch))
    return added


def generate_for_material(material_id):
    """Generate study guide + cards for one material. Returns (added, skipped)."""
    m = db.query_one("SELECT * FROM materials WHERE id=?", (material_id,))
    if not m:
        return 0, "no material"

    # poison-pill guard shared with ingest: total claude tries per material are
    # capped across BOTH loops. /retry and /regenerate reset the counter.
    if db.material_attempts(material_id) >= db.max_material_attempts():
        db.write("UPDATE materials SET status='failed', error_message=? "
                 "WHERE id=?", (FAIL_EXHAUSTED_MSG, material_id))
        return 0, "attempts exhausted"

    course_id = m["course_id"]
    text = m["extracted_text"]
    if not course_id or not text:
        db.write("UPDATE materials SET status='done' WHERE id=?", (material_id,))
        return 0, "no course/text"

    course = db.query_one("SELECT * FROM courses WHERE id=?", (course_id,))
    stype = course["subject_type"] if course else "memo"
    if stype == "other":  # 実技系: tracker only
        db.write("UPDATE materials SET status='done' WHERE id=?", (material_id,))
        return 0, "other -> tracker only"

    prompt_path = PROMPTS.get(stype, PROMPTS["memo"])
    with open(prompt_path, encoding="utf-8") as f:
        prompt = f.read().format(text=text)
    model = db.load_settings().get("model_text")

    db.write("UPDATE materials SET status='generating', error_message=NULL "
             "WHERE id=?", (material_id,))
    db.bump_material_attempts(material_id)  # count BEFORE the call (crash-safe)
    try:
        obj = _generate_with_retry(prompt, model)
    except ai.ClaudeError as e:
        db.write("UPDATE materials SET status='failed', error_message=? WHERE id=?",
                 (f"生成失敗: {str(e)[:RAW_PREVIEW]}", material_id))
        return 0, "claude error"
    except ValueError as e:
        db.write("UPDATE materials SET status='failed', error_message=? WHERE id=?",
                 (f"生成結果を読めませんでした: {str(e)[:RAW_PREVIEW]}", material_id))
        return 0, "parse error"

    guide = obj.get("study_guide_md")
    if guide:
        scope = None
        try:
            scope = json.loads(m["extracted_json"] or "{}").get("summary")
        except (TypeError, ValueError):
            pass
        db.write(
            "INSERT INTO study_guides (course_id, scope_desc, content_md, created_at) "
            "VALUES (?, ?, ?, ?)",
            (course_id, scope, guide, db.now_utc_iso()))

    added = _insert_generated_cards(course_id, material_id,
                                    obj.get("cards") or [], text)
    db.write("UPDATE materials SET status='done' WHERE id=?", (material_id,))
    return added, "ok"


def generate_handler(payload):
    generate_for_material(payload["material_id"])


worker.register("generate", generate_handler)
