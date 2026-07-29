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
import math
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
RECAST_PROMPT = os.path.join(BASE_DIR, "prompts", "recast.txt")
QUIZ_PROMPT = os.path.join(BASE_DIR, "prompts", "quiz.txt")
SUMMARY_PROMPT = os.path.join(BASE_DIR, "prompts", "summary.txt")

# Phase I quiz sizing: start ~1 question per QUIZ_CHARS_PER_Q chars of material,
# rounded UP to a multiple of QUIZ_STEP, clamped to [QUIZ_MIN, QUIZ_MAX]. If a big
# range still isn't covered, the user extends by +QUIZ_STEP from the UI.
QUIZ_MIN = 10
QUIZ_STEP = 10
QUIZ_MAX = 50
QUIZ_CHARS_PER_Q = 220

# Output-language directive injected into every generation prompt ({lang_line}).
# Bilingual + emphatic so it actually overrides a Japanese prompt's pull toward
# Japanese output. 'auto' matches the input; 'ja'/'en' force (and translate).
LANG_DIRECTIVE = {
    "auto": ("出力は入力（上の内容）と同じ言語で書く。英語なら英語、日本語なら日本語、"
             "その他の言語ならその言語。/ IMPORTANT: write ALL output in the SAME "
             "language as the input content above (English input -> answer fully "
             "in English; Japanese input -> Japanese)."),
    "ja": "出力は必ず日本語で書く（入力が何語でも日本語に翻訳して書く）。/ Write ALL output in Japanese.",
    "en": "Write ALL output in English (translate it if the input is in another language).",
}


def _lang_line(lang=None):
    """Resolve the output-language directive. `lang` None -> the saved global
    setting (db.content_lang). Unknown values fall back to 'auto'."""
    if lang not in db.VALID_CONTENT_LANGS:
        lang = db.content_lang()
    return LANG_DIRECTIVE.get(lang, LANG_DIRECTIVE["auto"])
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


# H1: modality types the generator may stamp + how each renders (see app.js
# renderCard). media_json is EXCLUDED from the D-5 content_hash (H0), so richer
# modalities never disturb dedup/regenerate.
_PRODUCE_TYPES = {"produce", "explain", "interpret", "predict", "compare", "elaborate"}
_STEP_TYPES = {"steps", "worked", "compute"}   # render an ordered step list
VALID_CARD_TYPES = {"qa", "term", "cloze", "list"} | _PRODUCE_TYPES | _STEP_TYPES


def _clean_card_type(ct):
    ct = (ct or "").strip().lower()
    return ct if ct in VALID_CARD_TYPES else "qa"


def _clean_media_json(card_type, mj):
    """Keep only the render structure a card_type actually uses (steps / items /
    rubric); drop everything else so a stray or huge blob can't slip in. Returns a
    JSON string or None. NEVER enters the content_hash — front/back are identity."""
    if not isinstance(mj, dict):
        return None
    out = {}
    if card_type in _STEP_TYPES:
        raw = mj.get("steps")
        if isinstance(raw, list):     # a string here would iterate per-character
            steps = [str(s).strip() for s in raw if str(s).strip()]
            if steps:
                out["steps"] = steps[:20]
    if card_type == "list":
        raw = mj.get("items")
        if isinstance(raw, list):
            items = [str(s).strip() for s in raw if str(s).strip()]
            if items:
                out["items"] = items[:30]
    if card_type in _PRODUCE_TYPES:
        rub = mj.get("rubric")
        if isinstance(rub, str) and rub.strip():
            out["rubric"] = rub.strip()[:600]
    return json.dumps(out, ensure_ascii=False) if out else None


def _insert_generated_cards(course_id, material_id, cards, extracted_text=""):
    now = db.now_utc_iso()
    added = 0
    for c in cards:
        front = (c.get("front") or "").strip()
        back = (c.get("back") or "").strip()
        if not front or not back:
            continue
        conf = c.get("confidence") if c.get("confidence") in ("high", "low") else None
        ctype = _clean_card_type(c.get("card_type"))
        media_json = _clean_media_json(ctype, c.get("media_json"))
        # E5: a verbatim quote lets the viewer show WHERE this card came from.
        sq = (c.get("source_quote") or "").strip() or None
        loc_json = (json.dumps(db.locate(sq, extracted_text), ensure_ascii=False)
                    if sq else None)
        ch = db.content_hash(front, back)
        cur = db.write_returning(
            """INSERT OR IGNORE INTO cards
               (course_id, material_id, card_type, front, back, topic, origin,
                confidence, source_quote, source_loc, media_json, content_hash, state,
                repetitions, current_interval, current_ease, created_at)
               VALUES (?,?,?,?,?,?, 'generated', ?,?,?,?,?, 'proposed', 0, 0, 2.5, ?)""",
            (course_id, material_id, ctype, front, back,
             c.get("topic"), conf, sq, loc_json, media_json, ch, now))
        if cur.rowcount:
            added += 1
            continue
        # Card already exists (D-5 dedup). Enrich WITHOUT touching front/back/SRS
        # state (keeps review history): E5 refreshes the source location; H1
        # backfills modality structure only when the card has none yet.
        if sq:
            db.write("UPDATE cards SET source_quote=?, source_loc=? "
                     "WHERE course_id IS ? AND content_hash=?",
                     (sq, loc_json, course_id, ch))
        if media_json:
            db.write("UPDATE cards SET media_json=COALESCE(media_json, ?) "
                     "WHERE course_id IS ? AND content_hash=?",
                     (media_json, course_id, ch))
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
        prompt = f.read().format(text=text, lang_line=_lang_line())
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

    # Persisting the guide + cards is wrapped so a malformed payload can never
    # leave the material stuck in 'generating' (the worker swallows exceptions);
    # any failure here marks it 'failed' with a message the UI can surface.
    try:
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
    except Exception as e:
        db.write("UPDATE materials SET status='failed', error_message=? WHERE id=?",
                 (f"カード保存に失敗しました: {str(e)[:RAW_PREVIEW]}", material_id))
        return 0, "insert error"
    db.write("UPDATE materials SET status='done' WHERE id=?", (material_id,))
    return added, "ok"


def generate_handler(payload):
    generate_for_material(payload["material_id"])


def recast_card(card_id, method):
    """Phase G2: AI-recast a card into `method` (a resolved method dict). Creates a
    NEW card in the target format (same course/material/source), suspends the
    original (recoverable), and returns the new card row. Raises on AI/parse error.

    The recast enters as a fresh 'new' card so it surfaces in the queue immediately
    regardless of the original's SRS state (a 'review'-state clone would carry a
    NULL next_due_at and be orphaned). The original is suspended ONLY once the new
    card is confirmed inserted — a content_hash collision (INSERT OR IGNORE no-op)
    or an identical recast must never suspend the source with no replacement.
    """
    card = db.query_one("SELECT * FROM cards WHERE id=?", (card_id,))
    if not card:
        return None
    with open(RECAST_PROMPT, encoding="utf-8") as f:
        prompt = f.read().format(
            front=card["front"], back=card["back"],
            method_name=method["name"], instruction=method["instruction"],
            lang_line=_lang_line())
    obj = _generate_with_retry(prompt, db.load_settings().get("model_text"))
    front = (obj.get("front") or "").strip()
    back = (obj.get("back") or "").strip()
    if not front or not back:
        raise ValueError("変換結果が空でした")
    ch = db.content_hash(front, back)
    if ch == card["content_hash"]:
        raise ValueError("変換結果が元のカードと同一でした")
    cur = db.write_returning(
        """INSERT OR IGNORE INTO cards
           (course_id, material_id, card_type, front, back, topic, origin,
            source_quote, source_loc, content_hash, state, repetitions,
            current_interval, current_ease, created_at)
           VALUES (?,?,?,?,?,?, 'generated', ?, ?, ?, 'new', 0, 0, 2.5, ?)""",
        (card["course_id"], card["material_id"], method["card_type"], front, back,
         card["topic"], card["source_quote"], card["source_loc"], ch,
         db.now_utc_iso()))
    if cur.rowcount != 1:
        raise ValueError("変換結果が既存カードと重複していました")
    db.write("UPDATE cards SET state='suspended' WHERE id=?", (card_id,))
    return db.query_one("SELECT * FROM cards WHERE course_id IS ? AND content_hash=?",
                        (card["course_id"], ch))


# --------------------------------------------------------------------------
# Phase I — material study modes: quiz (comprehension test) + summary (要点まとめ)
# --------------------------------------------------------------------------
def _auto_quiz_count(text):
    n = max(QUIZ_MIN, math.ceil(len(text or "") / QUIZ_CHARS_PER_Q))
    n = int(math.ceil(n / QUIZ_STEP) * QUIZ_STEP)
    return max(QUIZ_MIN, min(QUIZ_MAX, n))


def _material_text_or_raise(material_id):
    """Return (material_row, extracted_text) or (None, None) if the material is
    gone. Raises ValueError if the material exists but has no transcription yet."""
    m = db.query_one("SELECT * FROM materials WHERE id=?", (material_id,))
    if not m:
        return None, None
    text = (m["extracted_text"] or "").strip()
    if not text:
        raise ValueError("この教材はまだ文字起こしされていません（解析の完了後に使えます）")
    return m, text


def _clean_quiz_questions(raw, fmt):
    """Validate/normalize the model's questions. A 'choice' needs >=2 options with
    the answer among them, else it's demoted to 'written'. In 'written' format
    every question is written. Drops anything without a question+answer."""
    out = []
    for q in (raw or []):
        if not isinstance(q, dict):
            continue
        question = (q.get("question") or "").strip()
        answer = (q.get("answer") or "").strip()
        if not question or not answer:
            continue
        qtype, choices = q.get("type"), None
        if fmt == "mixed" and qtype == "choice":
            opts = [str(c).strip() for c in (q.get("choices") or []) if str(c).strip()]
            if len(opts) >= 2 and answer in opts:
                qtype, choices = "choice", opts
            else:
                qtype = "written"
        else:
            qtype = "written"
        item = {"type": qtype, "question": question, "answer": answer}
        if choices:
            item["choices"] = choices
        out.append(item)
    return out


def generate_quiz(material_id, fmt="written", count=None, scope=None, lang=None):
    """Phase I: generate a comprehension quiz over a material (one AI call, sync).
    fmt = 'written' (all self-graded short-answer) | 'mixed' (AI picks written vs
    4-choice per question). count None = auto-size from material length. lang None
    = the saved output-language setting. Stores + returns the quiz row. Raises
    ValueError on empty text / parse failure."""
    m, text = _material_text_or_raise(material_id)
    if not m:
        return None
    fmt = "mixed" if fmt == "mixed" else "written"
    n = max(QUIZ_MIN, min(QUIZ_MAX, int(count))) if count else _auto_quiz_count(text)
    scope = (scope or "").strip() or None
    format_line = (
        "各問題ごとに、概念理解を問うものは type=\"written\"（記述式）、事実確認は "
        "type=\"choice\"（4択）を選び、両方をバランス良く混ぜる。"
        if fmt == "mixed" else
        "すべての問題を type=\"written\"（記述式）にする。choices は付けない。")
    scope_line = (f"特に次の範囲に集中する: {scope}" if scope
                  else "教材全体を範囲とする。")
    with open(QUIZ_PROMPT, encoding="utf-8") as f:
        prompt = f.read().format(material=text, count=n, format_line=format_line,
                                 scope_line=scope_line, lang_line=_lang_line(lang))
    obj = _generate_with_retry(prompt, db.load_settings().get("model_text"))
    questions = _clean_quiz_questions(obj.get("questions"), fmt)
    if not questions:
        raise ValueError("問題を生成できませんでした")
    qid = db.write(
        "INSERT INTO quizzes (material_id, course_id, format, scope_desc, "
        "questions_json, created_at) VALUES (?,?,?,?,?,?)",
        (material_id, m["course_id"], fmt, scope,
         json.dumps(questions, ensure_ascii=False), db.now_utc_iso()))
    return db.query_one("SELECT * FROM quizzes WHERE id=?", (qid,))


def generate_summary(material_id, scope=None, lang=None):
    """Phase I: summarize a material into a Markdown study guide (要点まとめ).
    Synchronous. lang None = the saved output-language setting. Stores + returns
    the study_guides row."""
    m, text = _material_text_or_raise(material_id)
    if not m:
        return None
    scope = (scope or "").strip() or None
    scope_line = (f"特に次の範囲に集中する: {scope}" if scope
                  else "教材全体を対象とする。")
    with open(SUMMARY_PROMPT, encoding="utf-8") as f:
        prompt = f.read().format(material=text, scope_line=scope_line,
                                 lang_line=_lang_line(lang))
    obj = _generate_with_retry(prompt, db.load_settings().get("model_text"))
    md = (obj.get("summary_md") or "").strip()
    if not md:
        raise ValueError("まとめを生成できませんでした")
    gid = db.write(
        "INSERT INTO study_guides (course_id, material_id, scope_desc, "
        "content_md, created_at) VALUES (?,?,?,?,?)",
        (m["course_id"], material_id, scope, md, db.now_utc_iso()))
    return db.query_one("SELECT * FROM study_guides WHERE id=?", (gid,))


def generate_draft(material_id, instruction=None, scope=None):
    """Phase H3: on-demand 'make review cards from this material' with an optional
    free-text instruction and range. Reuses the subject prompt (so cards get H1
    modalities) plus a top-priority directive block for the steer/range, and inserts
    the cards as PROPOSED — nothing is studied until the user confirms (G1 gate).
    Returns {added, topics}. Raises ValueError on empty text / no course / parse."""
    m, text = _material_text_or_raise(material_id)
    if not m:
        return None
    course_id = m["course_id"]
    if not course_id:
        raise ValueError("この教材にはコース（科目）が紐づいていません。先に科目を割り当ててください。")
    course = db.query_one("SELECT * FROM courses WHERE id=?", (course_id,))
    stype = course["subject_type"] if course else "memo"
    if stype == "other":                      # tracker-only course -> memo style on demand
        stype = "memo"
    instruction = (instruction or "").strip() or None
    scope = (scope or "").strip() or None
    with open(PROMPTS.get(stype, PROMPTS["memo"]), encoding="utf-8") as f:
        prompt = f.read().format(text=text, lang_line=_lang_line())
    extra = []
    if scope:
        extra.append(f"範囲を次に限定する（この部分だけからカードを作る）：{scope}")
    if instruction:
        extra.append(f"利用者の指示（最優先で反映する）：{instruction}")
    if extra:
        prompt += "\n\n【重要な追加指示（最優先で従う）】\n- " + "\n- ".join(extra)
    obj = _generate_with_retry(prompt, db.load_settings().get("model_text"))
    cards = obj.get("cards") or []
    added = _insert_generated_cards(course_id, material_id, cards, text)
    topics = []
    for c in cards:
        t = (c.get("topic") or "").strip()
        if t and t not in topics:
            topics.append(t)
    return {"added": added, "topics": topics[:8]}


worker.register("generate", generate_handler)
