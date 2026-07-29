"""Priority scoring (feature 4).

score = w1*grade_impact + w2*deadline + w3*not_started
  - each term clipped to [0.1, 1.0] (weighted SUM, never a product — a product
    collapses to 0 and is unexplainable)
  - grade_impact = clip( (max_points / Σ max_points[same course + same
    category]) * category_weight , 0.1, 1.0 )                        (D-2)
      denominator = category-internal sum -> stable, no upfront config needed.
      category_weight from courses.weight_config_json; unset -> 1.0 (equal).
  - deadline   = urgency from remaining time. Overdue items are ISOLATED into a
    separate list (NOT max-clamped into the ranking, which would let
    un-submittable past items squat at the top forever).
  - not_started = status coefficient (in-progress kept > 0, not collapsed).

Big tasks (estimated_minutes >= big_task_minutes) are urged from their
"start-recommend date" (= due - N days), so multi-hour work surfaces early.

The UI never shows the numeric score — only a natural-language "why #1".
"""

import json
from datetime import timedelta

import db

CAT_JP = {"homework": "宿題", "quiz": "小テスト", "exam": "試験",
          "project": "レポート", "other": "その他"}
HORIZON_H = 168.0  # 1 week: due within a week starts registering urgency
STATUS_COEF = {"todo": 1.0, "in_progress": 0.5}


def _clip(x, lo=0.1, hi=1.0):
    return max(lo, min(hi, x))


def _settings():
    s = db.load_settings()
    pw = s.get("priority_weights") or {}
    w1 = float(pw.get("grade_impact", 0.5))
    w2 = float(pw.get("deadline", 0.35))
    w3 = float(pw.get("not_started", 0.15))
    return (w1, w2, w3,
            int(s.get("big_task_minutes", 180)),
            int(s.get("start_recommend_days_before", 3)))


def _category_weight(course_weights, category):
    if course_weights and category in course_weights:
        try:
            return float(course_weights[category])
        except (TypeError, ValueError):
            return 1.0
    return 1.0  # unset -> equal (don't force config)


def _grade_impact(a, denom_map, course_weights):
    mp = a["max_points"] or 0.0
    denom = denom_map.get((a["course_id"], a["category"]), 0.0) or 0.0
    ratio = (mp / denom) if denom > 0 else 1.0
    w = _category_weight(course_weights.get(a["course_id"]), a["category"])
    return _clip(ratio * w)


def _deadline_urgency(a, now, big_minutes, start_days):
    due = db.parse_iso(a["due_at"])
    if due is None:
        return 0.3  # no deadline -> low-mid, never dominant
    eff = due
    if (a["estimated_minutes"] or 0) >= big_minutes:
        eff = due - timedelta(days=start_days)  # surface big tasks earlier
    rem_h = (eff - now).total_seconds() / 3600.0
    if rem_h <= 0:
        return 1.0
    return _clip(1.0 - rem_h / HORIZON_H)


def _not_started(a):
    return _clip(STATUS_COEF.get(a["status"], 0.3))


def _human_due(iso):
    d = db.to_local(iso)
    if d is None:
        return "期限なし"
    now = db.to_local(db.now_utc_iso())
    h = (d - now).total_seconds() / 3600.0
    if h < 0:
        return f"{abs(int(h))}時間 超過" if -h < 24 else f"{int(-h // 24)}日 超過"
    if h <= 48:
        return "まもなく" if h < 1 else f"残り{round(h)}時間"
    return d.strftime("%-m/%-d")


def _reason(gi, du, ns, weights, a):
    w1, w2, w3 = weights
    terms = {"grade": w1 * gi, "deadline": w2 * du, "not_started": w3 * ns}
    top = max(terms, key=terms.get)
    due_txt = _human_due(a["due_at"])
    if top == "grade":
        pts = a["max_points"]
        pts_txt = f"配点{int(pts)}" if pts else "配点大"
        primary = f"{pts_txt}・{CAT_JP.get(a['category'], a['category'])}"
    elif top == "deadline":
        primary = "締切が近い"
    else:
        primary = "未着手"
    return f"{primary} ・ {due_txt}"


def build_today():
    now = db.now_dt()
    now_iso = db.now_utc_iso()
    weights = _settings()
    w1, w2, w3, big_minutes, start_days = weights
    wtuple = (w1, w2, w3)

    # category-internal denominator over ALL assignments (stable) — D-2
    denom_map = {}
    for r in db.query(
            "SELECT course_id, category, COALESCE(SUM(max_points),0) s "
            "FROM assignments GROUP BY course_id, category"):
        denom_map[(r["course_id"], r["category"])] = r["s"]

    course_weights = {}
    for c in db.query("SELECT id, weight_config_json FROM courses"):
        try:
            course_weights[c["id"]] = (
                json.loads(c["weight_config_json"])
                if c["weight_config_json"] else None)
        except (TypeError, ValueError):
            course_weights[c["id"]] = None

    rows = db.query(
        db.ASSIGN_JOIN + " WHERE a.status IN ('todo','in_progress')")

    overdue, active = [], []
    for r in rows:
        if r["due_at"] and r["due_at"] < now_iso:
            overdue.append(r)
        else:
            active.append(r)

    scored = []
    for a in active:
        gi = _grade_impact(a, denom_map, course_weights)
        du = _deadline_urgency(a, now, big_minutes, start_days)
        ns = _not_started(a)
        score = w1 * gi + w2 * du + w3 * ns
        scored.append({"a": a, "score": score, "gi": gi, "du": du, "ns": ns})

    # pinned first (manual override / algorithm escape hatch), then score desc
    scored.sort(key=lambda x: (bool(x["a"]["pinned"]), x["score"]), reverse=True)

    overdue.sort(key=lambda r: r["due_at"])

    focus = scored[0] if scored else None
    focus_reason = (_reason(focus["gi"], focus["du"], focus["ns"], wtuple,
                            focus["a"]) if focus else None)

    review_due = db.query_one(
        "SELECT COUNT(*) n FROM cards WHERE state NOT IN ('suspended','proposed') "
        "AND (state = 'new' OR (next_due_at IS NOT NULL AND next_due_at <= ?))",
        (now_iso,))["n"]

    return {
        "focus": db.assignment_dict(focus["a"]) if focus else None,
        "focus_reason": focus_reason,
        "todo": [db.assignment_dict(x["a"]) for x in scored],
        "overdue": [db.assignment_dict(r) for r in overdue],
        "review_due": review_due,
    }
