"""Self-hosted spaced repetition (feature 3) — simplified SM-2.

Two-layer: update the card's CURRENT state (state/next_due_at/repetitions/
current_interval/current_ease) AND append an immutable row to `reviews`.

SM-2 (engineer-reviewed fixes):
- `repetitions` (consecutive-correct count n) is a dedicated column — required
  because a 4-button UI can't reconstruct n from the interval.
- 4 buttons -> quality q, decided here:
    again = lapse (repetitions->0, interval->1 day/翌日; EF still updated & clamped>=1.3)
    hard  = q3,  good = q4,  easy = q5
- No minute-level learning steps (simplified): `again` = next day.
- "Today's review" = (next_due_at <= now OR state='new') AND state != 'suspended'
  (suspended cards must not leak out via a past next_due_at).

Anti-pileup UX (why self-hosted SRS survives): caps (new + due), never expose the
grand total, and day-spread overdue cards (redistribute) instead of dumping them.
"""

import json
from datetime import timedelta

import db

QUALITY = {"again": 1, "hard": 3, "good": 4, "easy": 5}


def _ef_update(ef, q):
    ef2 = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    return max(1.3, round(ef2, 4))


def answer(card_id, grade):
    """Apply a review grade: update the card + append a reviews row."""
    q = QUALITY.get(grade)
    if q is None:
        raise ValueError(f"bad grade: {grade}")
    c = db.query_one("SELECT * FROM cards WHERE id=?", (card_id,))
    if not c:
        raise ValueError("no card")

    ef = _ef_update(c["current_ease"] or 2.5, q)
    if q < 3:  # again -> lapse
        reps = 0
        interval = 1
    else:
        reps = c["repetitions"] or 0
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 6
        else:
            interval = max(1, round((c["current_interval"] or 1) * ef))
        reps += 1

    next_due = db.now_dt() + timedelta(days=interval)
    next_due_iso = db.to_utc_iso(next_due)
    # Card update + review append are one transaction: a mid-way failure must
    # not leave the card advanced without its immutable reviews row (or vice
    # versa). write_many commits both or rolls both back (C2).
    db.write_many([
        ("UPDATE cards SET state='review', repetitions=?, current_interval=?, "
         "current_ease=?, next_due_at=? WHERE id=?",
         (reps, interval, ef, next_due_iso, card_id)),
        ("INSERT INTO reviews (card_id, reviewed_at, grade, interval_days, "
         "ease_factor) VALUES (?, ?, ?, ?, ?)",
         (card_id, db.now_utc_iso(), grade, interval, ef)),
    ])
    return {"card_id": card_id, "grade": grade, "repetitions": reps,
            "interval_days": interval, "ease": ef, "next_due_at": next_due_iso}


CARD_JOIN = (
    "SELECT c.*, co.name AS course_name, co.subject_type AS subject_type, "
    "m.original_path AS thumb_path "
    "FROM cards c "
    "LEFT JOIN courses co ON co.id = c.course_id "
    "LEFT JOIN materials m ON m.id = c.material_id")


def _exam_soon_courses(now_iso, within_days=7):
    horizon = db.to_utc_iso(db.parse_iso(now_iso) + timedelta(days=within_days))
    rows = db.query(
        "SELECT DISTINCT course_id FROM assignments WHERE category='exam' "
        "AND status IN ('todo','in_progress') AND due_at IS NOT NULL "
        "AND due_at >= ? AND due_at <= ?", (now_iso, horizon))
    return {r["course_id"] for r in rows if r["course_id"]}


def _why_now(card, exam_courses):
    if card["course_id"] in exam_courses:
        return "テスト範囲"
    if card["state"] == "new":
        return "新規カード"
    last = db.query_one(
        "SELECT grade FROM reviews WHERE card_id=? ORDER BY id DESC LIMIT 1",
        (card["id"],))
    if last and last["grade"] == "again":
        return "前回×"
    return "復習日"


def _card_out(r, exam_courses):
    keys = r.keys()
    return {
        "id": r["id"], "front": r["front"], "back": r["back"],
        "topic": r["topic"], "origin": r["origin"], "confidence": r["confidence"],
        "source_quote": r["source_quote"], "course_name": r["course_name"],
        "subject_type": r["subject_type"], "state": r["state"],
        # H0: modality — the review screen branches on card_type + media_json.
        "card_type": r["card_type"],
        "media_json": (json.loads(r["media_json"])
                       if ("media_json" in keys and r["media_json"]) else None),
        # J: cached ja/en translations so a language-flip in review is instant.
        "translation": (json.loads(r["translation_json"])
                        if ("translation_json" in keys and r["translation_json"]) else None),
        # E5: let the review screen jump back to WHERE this card came from.
        "material_id": r["material_id"],
        "source_loc": (json.loads(r["source_loc"])
                       if ("source_loc" in keys and r["source_loc"]) else None),
        "thumb_url": ("/" + r["thumb_path"]) if r["thumb_path"] else None,
        "why_now": _why_now(r, exam_courses),
    }


def get_queue():
    """Due + new cards, capped. Grand total is intentionally NOT returned."""
    s = db.load_settings()
    new_cap = int(s.get("review_new_cap", 10))
    due_cap = int(s.get("review_due_cap", 20))
    now_iso = db.now_utc_iso()
    exam_courses = _exam_soon_courses(now_iso)
    due = db.query(
        CARD_JOIN + " WHERE c.state='review' AND c.next_due_at IS NOT NULL "
        "AND c.next_due_at <= ? ORDER BY c.next_due_at ASC LIMIT ?",
        (now_iso, due_cap))
    new = db.query(
        CARD_JOIN + " WHERE c.state='new' ORDER BY c.id ASC LIMIT ?", (new_cap,))
    cards = [_card_out(r, exam_courses) for r in due] + \
            [_card_out(r, exam_courses) for r in new]
    return {"cards": cards, "new_count": len(new), "due_count": len(due)}


def counts():
    now_iso = db.now_utc_iso()
    n = db.query_one(
        "SELECT COUNT(*) n FROM cards WHERE state NOT IN ('suspended','proposed') "
        "AND (state='new' OR (next_due_at IS NOT NULL AND next_due_at <= ?))",
        (now_iso,))["n"]
    return n


def report_verified(card_id, verdict):
    """User pressed 'この問題おかしい'."""
    if verdict not in ("ok", "wrong"):
        raise ValueError("bad verdict")
    db.write("UPDATE cards SET verified=? WHERE id=?", (verdict, card_id))


def redistribute(days=7):
    """Day-spread overdue cards so returning after a break doesn't dump them all."""
    days = max(1, int(days))  # guard: days=0 would divide-by-zero on (i % days)
    now = db.now_dt()
    now_iso = db.now_utc_iso()
    overdue = db.query(
        "SELECT id FROM cards WHERE state='review' AND next_due_at IS NOT NULL "
        "AND next_due_at < ? ORDER BY next_due_at ASC", (now_iso,))
    for i, c in enumerate(overdue):
        newdue = now + timedelta(days=(i % days))
        db.write("UPDATE cards SET next_due_at=? WHERE id=?",
                 (db.to_utc_iso(newdue), c["id"]))
    return len(overdue)


# --------------------------------------------------------------------------
# Test mode (cram) — the Phase-4 centerpiece. Uses the tracker's exam data.
# Ignores SM-2 spacing; front-loads the range across the days until the exam.
# Stateless (recomputed daily): as the exam nears, days shrink -> more per day.
# --------------------------------------------------------------------------
def _cram_candidates(course_id=None, topic=None):
    where, params = ["c.state NOT IN ('suspended','proposed')"], []
    if course_id:
        where.append("c.course_id = ?")
        params.append(course_id)
    if topic:
        where.append("c.topic = ?")
        params.append(topic)
    return db.query(
        CARD_JOIN + " WHERE " + " AND ".join(where) + " ORDER BY c.id", params)


def cram_plan(exam_date_iso, course_id=None, topic=None):
    """Return the full day-by-day distribution + today's batch."""
    cards = _cram_candidates(course_id, topic)
    exam = db.parse_iso(exam_date_iso)
    today = db.now_dt().date()
    days = max(1, (exam.date() - today).days)
    per_day = [[] for _ in range(days)]
    for i, c in enumerate(cards):
        per_day[i % days].append(c["id"])
    now_iso = db.now_utc_iso()
    exam_courses = _exam_soon_courses(now_iso)
    today_ids = set(per_day[0]) if per_day else set()
    return {
        "exam_date": exam_date_iso,
        "days_left": days,
        "total_cards": len(cards),
        "per_day_counts": [len(d) for d in per_day],
        "today": [_card_out(r, exam_courses) for r in cards if r["id"] in today_ids],
    }


def cram_for_assignment(assignment_id):
    a = db.query_one("SELECT * FROM assignments WHERE id=?", (assignment_id,))
    if not a or not a["due_at"]:
        raise ValueError("exam assignment not found or has no date")
    return cram_plan(a["due_at"], course_id=a["course_id"])


# --------------------------------------------------------------------------
# Mastery (%) and weak-card isolation + drill
# --------------------------------------------------------------------------
def mastery(course_id=None):
    where, params = ["state NOT IN ('suspended','proposed')"], []
    if course_id:
        where.append("course_id = ?")
        params.append(course_id)
    cards = db.query(
        "SELECT repetitions FROM cards WHERE " + " AND ".join(where), params)
    if not cards:
        return {"pct": 0, "total": 0, "mastered": 0}
    mastered = sum(1 for c in cards if (c["repetitions"] or 0) >= 2)
    return {"pct": round(100 * mastered / len(cards)),
            "total": len(cards), "mastered": mastered}


def weak_cards(limit=5):
    now_iso = db.now_utc_iso()
    exam_courses = _exam_soon_courses(now_iso)
    rows = db.query(
        CARD_JOIN + " WHERE c.state NOT IN ('suspended','proposed') AND (c.verified='wrong' OR "
        "c.id IN (SELECT card_id FROM reviews WHERE grade='again' "
        "GROUP BY card_id HAVING COUNT(*) >= 2)) ORDER BY c.id LIMIT ?",
        (limit,))
    return [_card_out(r, exam_courses) for r in rows]


def drill(course_id=None, topic=None, limit=20):
    where, params = ["c.state NOT IN ('suspended','proposed')"], []
    if course_id:
        where.append("c.course_id = ?")
        params.append(course_id)
    if topic:
        where.append("c.topic = ?")
        params.append(topic)
    now_iso = db.now_utc_iso()
    exam_courses = _exam_soon_courses(now_iso)
    rows = db.query(
        CARD_JOIN + " WHERE " + " AND ".join(where) +
        " ORDER BY RANDOM() LIMIT ?", params + [limit])
    return [_card_out(r, exam_courses) for r in rows]


# --------------------------------------------------------------------------
# Phase J — study by (study) material: the review home groups study-ready cards by
# the material they came from so a learner can pick ONE material to review, or
# select several and merge them into one session. Deliberately study-ALL (like a
# drill), not the spaced queue — picking a material means "study its cards now".
# --------------------------------------------------------------------------
def review_materials():
    """Materials (and a 教材なし bucket for material-less cards) that have study-ready
    cards, with new/due/total counts — the data behind the by-material picker."""
    now_iso = db.now_utc_iso()
    # NOTE: a material's display "summary" is derived from extracted_json (there is
    # no summary column) — mirror ingest.material_dict and parse it in Python.
    rows = db.query(
        "SELECT c.material_id AS material_id, m.extracted_json AS extracted_json, "
        "m.kind AS kind, m.original_path AS thumb_path, co.name AS course_name, "
        "co.subject_type AS subject_type, COUNT(*) AS total, "
        "SUM(CASE WHEN c.state='new' THEN 1 ELSE 0 END) AS new_n, "
        "SUM(CASE WHEN c.state='review' AND c.next_due_at IS NOT NULL "
        "         AND c.next_due_at <= ? THEN 1 ELSE 0 END) AS due_n "
        "FROM cards c "
        "LEFT JOIN materials m ON m.id = c.material_id "
        "LEFT JOIN courses co ON co.id = c.course_id "
        "WHERE c.state NOT IN ('suspended','proposed') "
        "GROUP BY c.material_id "
        "ORDER BY (due_n + new_n) DESC, c.material_id DESC", (now_iso,))
    out = []
    for r in rows:
        mid = r["material_id"]
        summary = None
        if r["extracted_json"]:
            try:
                summary = (json.loads(r["extracted_json"]) or {}).get("summary")
            except (TypeError, ValueError):
                summary = None
        label = (summary or "").strip() or (
            f"教材 #{mid}" if mid else "教材なし（手動カードなど）")
        out.append({
            "material_id": mid,
            "label": label,
            "course_name": r["course_name"],
            "subject_type": r["subject_type"],
            "kind": r["kind"],
            "thumb_url": ("/" + r["thumb_path"]) if r["thumb_path"] else None,
            "total": r["total"], "new_count": r["new_n"] or 0, "due_count": r["due_n"] or 0,
        })
    return out


def by_materials(tokens, limit=400):
    # limit raised 80 -> 400 at the user's request: their TOEFL deck is 344 cards
    # and a by-material session should be able to cover the whole material.
    """A review queue drawn from one or more materials (merge). `tokens` are
    material-id strings; the literal 'none' selects material-less cards. Study-ALL
    (every non-suspended/proposed card of those materials), due-first then new."""
    want_null = any(str(t).strip().lower() in ("none", "null") for t in tokens)
    # isascii() guards against Unicode "digits" (e.g. '²') that pass isdigit() but
    # raise on int() — a public endpoint must not 500 on a crafted material_id.
    ids = [int(s) for t in tokens
           if (s := str(t).strip()).isascii() and s.isdigit() and int(s) > 0]
    if not ids and not want_null:
        return []
    clauses, params = [], []
    if ids:
        clauses.append("c.material_id IN (%s)" % ",".join("?" * len(ids)))
        params += ids
    if want_null:
        clauses.append("c.material_id IS NULL")
    now_iso = db.now_utc_iso()
    exam_courses = _exam_soon_courses(now_iso)
    rows = db.query(
        CARD_JOIN + " WHERE c.state NOT IN ('suspended','proposed') AND ("
        + " OR ".join(clauses) + ") "
        "ORDER BY (c.state='new'), (c.next_due_at IS NULL), c.next_due_at ASC, "
        "c.id ASC LIMIT ?", params + [limit])
    return [_card_out(r, exam_courses) for r in rows]


# --------------------------------------------------------------------------
# G3: session review strategies — a layer ON TOP of SM-2, not a replacement.
# Nothing here touches scheduling, card state or the D-5 identity: a strategy
# only changes the ORDER cards arrive in and how the UI asks the question. No
# schema change, so a strategy can be switched on or off per session freely.
# --------------------------------------------------------------------------
VALID_STRATEGIES = ("retrieval", "interleave", "recognition", "elaborate")


def clean_strategies(raw):
    """Normalize a strategy list from a query string, request body or settings."""
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for s in raw:
        s = str(s).strip().lower()
        if s in VALID_STRATEGIES and s not in out:
            out.append(s)
    return out


def interleave(cards):
    """Interleaving: consecutive cards should come from DIFFERENT materials, since
    blocked practice (all of one topic, then all of the next) feels easier but
    retains worse. Round-robins the material groups while preserving each group's
    own due-first order, so SM-2's urgency ordering survives within a topic.

    Deterministic — no RNG — so a reload does not reshuffle mid-session.
    """
    groups = {}
    order = []
    for c in cards:
        key = c.get("material_id")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)
    out = []
    i = 0
    while len(out) < len(cards):
        placed = False
        for key in order:
            g = groups[key]
            if i < len(g):
                out.append(g[i])
                placed = True
        if not placed:      # every group exhausted (guards a malformed input)
            break
        i += 1
    return out


def distractors_for(card_id, limit=3):
    """Recognition mode (出題形式: 入力↔再認): plausible wrong options for a card,
    taken from OTHER cards' answers in the same course. Drawn from real study
    material rather than invented, so no AI call and no new dependency.

    Prefers answers of a similar length to the real one — a conspicuously short or
    long option gives the answer away. Deterministic ordering.
    """
    card = db.query_one("SELECT * FROM cards WHERE id=?", (card_id,))
    if not card:
        return []
    real = (card["back"] or "").strip()
    rows = db.query(
        "SELECT back FROM cards WHERE course_id=? AND id<>? "
        "AND state NOT IN ('suspended','proposed') AND back IS NOT NULL",
        (card["course_id"], card_id))
    seen = {db.norm_text(real)}
    cand = []
    for r in rows:
        b = (r["back"] or "").strip()
        key = db.norm_text(b)
        if not b or key in seen:
            continue
        seen.add(key)
        cand.append(b)
    if not cand:
        return []
    target = len(real)
    cand.sort(key=lambda b: (abs(len(b) - target), b))
    return cand[:max(0, int(limit))]
