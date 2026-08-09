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
TRANSLATE_PROMPT = os.path.join(BASE_DIR, "prompts", "translate.txt")
GLOSSARY_PROMPT = os.path.join(BASE_DIR, "prompts", "glossary.txt")

# Phase K: per-material term table. Capped so the injected directive stays small
# next to the material itself — a glossary that crowds out the content is worse
# than none.
GLOSSARY_MAX_TERMS = 24
GLOSSARY_MIN_CHARS = 400   # too little text to be worth an AI call

# Phase J: card language-flip. Only ja<->en are offered in the review UI.
TRANSLATE_TARGETS = {"ja": "日本語 (Japanese)", "en": "英語 (English)"}

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

# Terminology discipline, appended to every directive. Forcing a language is not
# enough: a real-material eval on an English biology deck showed the model coining
# calques and katakana pseudo-terms rather than using the words a textbook uses
# (中心教義 for Central Dogma, ニトロゲンベース for nitrogenous base, テンプレート for
# the 鋳型 strand), and spelling one term two ways across surfaces (エクソン/エキソン).
TERM_RULE = (
    "専門用語は、その言語の教科書で実際に使われている標準的な用語を使う"
    "（直訳やカタカナ音訳を作らない。例: Central Dogma→セントラルドグマ、"
    "template→鋳型（「テンプレート」としない）、nitrogenous base→窒素塩基、"
    "transfer RNA→転移RNA）。"
    "同じ用語は最初から最後まで同じ表記に統一する。"
    "専門用語は1対1で訳し、上位語・下位語・近い概念に置き換えない"
    "（nucleotide=ヌクレオチド であって 塩基 ではない。base=塩基、strand=鎖、"
    "gene=遺伝子）。元の資料がより具体的な語を使っているなら、訳文も同じ具体度の語を使う。"
    "/ Use the term a textbook in the output language actually uses — never invent a "
    "literal calque or a transliteration when a real term exists — and spell each term "
    "identically throughout. Map technical terms one-to-one: never substitute a broader "
    "or narrower concept (nucleotide is NOT 'base'). If the source uses the more "
    "specific term, the output must use the equally specific term.")


def _lang_line(lang=None):
    """Resolve the output-language directive. `lang` None -> the saved global
    setting (db.content_lang). Unknown values fall back to 'auto'."""
    if lang not in db.VALID_CONTENT_LANGS:
        lang = db.content_lang()
    return LANG_DIRECTIVE.get(lang, LANG_DIRECTIVE["auto"]) + " " + TERM_RULE
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
    model = db.load_settings().get("model_text")

    db.write("UPDATE materials SET status='generating', error_message=NULL "
             "WHERE id=?", (material_id,))
    db.bump_material_attempts(material_id)  # count BEFORE the call (crash-safe)
    with open(prompt_path, encoding="utf-8") as f:
        # K: build the term table BEFORE the cards, so the cards themselves — the
        # primary study surface — set the vocabulary the summary and quiz then
        # follow, instead of each surface inventing its own. Built AFTER the
        # attempts bump: a glossary call that hangs or dies must not escape the
        # poison-pill cap and leave this material stuck in 'generating'.
        prompt = f.read().format(
            text=text, lang_line=_lang_line() + _glossary_line(material_id))
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
            # K: a recast rewrites a card in place — it must keep its material's
            # vocabulary. build=False: recast is an interactive click, not a good
            # moment to pay for a first glossary build.
            lang_line=_lang_line() + _glossary_line(card["material_id"], build=False))
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


def _card_translations(card):
    """Parse a card's cached translation blob (tolerant of pre-migration rows)."""
    if "translation_json" in card.keys() and card["translation_json"]:
        try:
            data = json.loads(card["translation_json"])
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def translate_card(card_id, target_lang):
    """Phase J: translate a card's front/back into `target_lang` ('ja'|'en') so a
    learner can flip its language while reviewing. The result is CACHED per-lang in
    cards.translation_json — which is EXCLUDED from the D-5 content_hash — so the SRS
    card is never duplicated or reset, and a re-flip is instant (no AI call). Returns
    {"lang","front","back","cached"} or None if the card is gone. Raises on bad lang
    / empty result / AI error."""
    if target_lang not in TRANSLATE_TARGETS:
        raise ValueError("翻訳先の言語が不正です")
    card = db.query_one("SELECT * FROM cards WHERE id=?", (card_id,))
    if not card:
        return None
    cache = _card_translations(card)
    hit = cache.get(target_lang)
    if isinstance(hit, dict) and (hit.get("front") or "").strip() and (hit.get("back") or "").strip():
        return {"lang": target_lang, "front": hit["front"], "back": hit["back"], "cached": True}
    with open(TRANSLATE_PROMPT, encoding="utf-8") as f:
        prompt = f.read().format(
            target=TRANSLATE_TARGETS[target_lang],
            # a translated card must match the terms its material's summary and
            # quiz already use, so the same glossary feeds this path too
            term_rule=TERM_RULE + _glossary_line(card["material_id"], build=False),
            front=card["front"], back=card["back"])
    obj = _generate_with_retry(prompt, db.load_settings().get("model_text"))
    front = (obj.get("front") or "").strip()
    back = (obj.get("back") or "").strip()
    if not front or not back:
        raise ValueError("翻訳結果が空でした")
    cache[target_lang] = {"front": front, "back": back}
    db.write("UPDATE cards SET translation_json=? WHERE id=?",
             (json.dumps(cache, ensure_ascii=False), card_id))
    return {"lang": target_lang, "front": front, "back": back, "cached": False}


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


def _gloss_str(v):
    """A model (or a hand-edited row) can put a number, list or null where a term
    string belongs. Anything not a string is simply not a term."""
    return v.strip() if isinstance(v, str) else ""


def _parse_glossary(material):
    """Read materials.glossary_json tolerantly -> list of {src, ja, en} dicts."""
    if material is None or "glossary_json" not in material.keys():
        return []
    try:
        obj = json.loads(material["glossary_json"] or "")
    except (TypeError, ValueError):
        return []
    terms = obj.get("terms") if isinstance(obj, dict) else obj
    if not isinstance(terms, list):
        return []
    out = []
    for t in terms:
        if not isinstance(t, dict):
            continue
        ja, en = _gloss_str(t.get("ja")), _gloss_str(t.get("en"))
        if ja and en:
            out.append({"src": _gloss_str(t.get("src")), "ja": ja, "en": en})
    return out[:GLOSSARY_MAX_TERMS]


def build_glossary(material_id, force=False):
    """Phase K: derive ONE canonical term table for a material and cache it on the
    row, so every later prompt about that material spells a term the same way.
    Cache-first; returns the term list (possibly empty). Never raises for an
    ordinary miss — a glossary is an enhancement, so callers degrade to no
    glossary rather than failing the generation the user actually asked for."""
    m = db.query_one("SELECT * FROM materials WHERE id=?", (material_id,))
    if not m:
        return []
    if not force and "glossary_json" in m.keys() and m["glossary_json"]:
        # A stored EMPTY term list still counts as built. Testing the parsed list
        # for truthiness instead would re-run the AI call on every generation for
        # any material whose glossary legitimately came back empty.
        return _parse_glossary(m)
    text = (m["extracted_text"] or "").strip()
    if len(text) < GLOSSARY_MIN_CHARS:
        return []   # deliberately NOT cached: the text may still be growing
    with open(GLOSSARY_PROMPT, encoding="utf-8") as f:
        prompt = f.read().format(material=text, limit=GLOSSARY_MAX_TERMS)
    obj = _generate_with_retry(prompt, db.load_settings().get("model_text"))
    terms = obj.get("terms") if isinstance(obj, dict) else None
    clean = []
    seen = set()
    for t in (terms if isinstance(terms, list) else []):
        if not isinstance(t, dict):
            continue
        ja, en = _gloss_str(t.get("ja")), _gloss_str(t.get("en"))
        key = ja.lower() + "\x1f" + en.lower()
        if not ja or not en or key in seen:
            continue
        seen.add(key)
        clean.append({"src": _gloss_str(t.get("src")), "ja": ja, "en": en})
        if len(clean) >= GLOSSARY_MAX_TERMS:
            break
    # A shapeless reply is cached as "built, no terms" like any other empty result:
    # returning early without writing would re-run this AI call on every later
    # generation for this material, forever.
    db.write("UPDATE materials SET glossary_json=? WHERE id=?",
             (json.dumps({"terms": clean}, ensure_ascii=False), material_id))
    return clean


def _glossary_line(material_id, build=True):
    """The term-table directive appended to a material's prompts. Returns "" when
    there is no glossary (or the AI is unavailable) so generation still works.

    The guard is a bare Exception on purpose. This helper is called while building
    someone else's prompt, and a glossary is only ever an enhancement — no failure
    in here may 500 an endpoint whose contract is a 200 {"ok": false}, or strand a
    material in 'generating' because it raised before the attempts counter bumped.
    """
    if not material_id:
        return ""
    try:
        m = db.query_one("SELECT * FROM materials WHERE id=?", (material_id,))
        terms = _parse_glossary(m)
        if not terms and build:
            terms = build_glossary(material_id)
    except Exception:   # noqa: BLE001 — deliberate: never break the caller
        return ""
    if not terms:
        return ""
    pairs = "、".join(f"{t['en']}={t['ja']}" for t in terms)
    return ("\n- この教材の用語表に必ず従う（ゆれを作らない）: " + pairs
            + " / Use exactly these term pairs for this material.")


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
        prompt = f.read().format(
            material=text, count=n, format_line=format_line, scope_line=scope_line,
            lang_line=_lang_line(lang) + _glossary_line(material_id))
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
        prompt = f.read().format(
            material=text, scope_line=scope_line,
            lang_line=_lang_line(lang) + _glossary_line(material_id))
    obj = _generate_with_retry(prompt, db.load_settings().get("model_text"))
    md = (obj.get("summary_md") or "").strip()
    if not md:
        raise ValueError("まとめを生成できませんでした")
    gid = db.write(
        "INSERT INTO study_guides (course_id, material_id, scope_desc, "
        "content_md, created_at) VALUES (?,?,?,?,?)",
        (m["course_id"], material_id, scope, md, db.now_utc_iso()))
    return db.query_one("SELECT * FROM study_guides WHERE id=?", (gid,))


def generate_draft(material_id, instruction=None, scope=None, lang=None):
    """Phase H3: on-demand 'make review cards from this material' with an optional
    free-text instruction and range. Reuses the subject prompt (so cards get H1
    modalities) plus a top-priority directive block for the steer/range, and inserts
    the cards as PROPOSED — nothing is studied until the user confirms (G1 gate).
    lang None = the saved output-language setting (auto follows the material).
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
        # K: same material, same term table as its summary/quiz/existing cards
        prompt = f.read().format(
            text=text, lang_line=_lang_line(lang) + _glossary_line(material_id))
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
