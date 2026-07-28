"""Pure-logic tests for priority.py, clock frozen via the `clock` fixture."""

from datetime import timedelta

import pytest

import db
import priority
from tests import seed
from tests.conftest import FIXED_NOW


# --------------------------------------------------------------------------
# grade_impact — category-internal denominator (D-2)
# --------------------------------------------------------------------------
def test_grade_impact_denominator_is_category_internal():
    denom = {(1, "homework"): 400.0}
    a = {"course_id": 1, "category": "homework", "max_points": 100.0}
    b = {"course_id": 1, "category": "homework", "max_points": 300.0}
    assert priority._grade_impact(a, denom, {}) == pytest.approx(0.25)
    assert priority._grade_impact(b, denom, {}) == pytest.approx(0.75)


def test_grade_impact_no_denominator_defaults_to_ratio_1():
    a = {"course_id": 1, "category": "exam", "max_points": 0.0}
    # denom 0 -> ratio 1.0, clipped to <=1.0
    assert priority._grade_impact(a, {}, {}) == 1.0


def test_grade_impact_applies_course_category_weight():
    denom = {(1, "quiz"): 100.0}
    a = {"course_id": 1, "category": "quiz", "max_points": 100.0}
    cw = {1: {"quiz": 0.5}}
    assert priority._grade_impact(a, denom, cw) == pytest.approx(0.5)


# --------------------------------------------------------------------------
# deadline urgency + big-task early-surface
# --------------------------------------------------------------------------
def test_deadline_none_is_low_mid():
    assert priority._deadline_urgency({"due_at": None, "estimated_minutes": 60},
                                      FIXED_NOW, 180, 3) == 0.3


def test_deadline_overdue_is_max():
    a = {"due_at": db.to_utc_iso(FIXED_NOW - timedelta(hours=1)),
         "estimated_minutes": 60}
    assert priority._deadline_urgency(a, FIXED_NOW, 180, 3) == 1.0


def test_big_task_surfaces_earlier_than_normal():
    due = db.to_utc_iso(FIXED_NOW + timedelta(days=5))
    normal = {"due_at": due, "estimated_minutes": 60}
    big = {"due_at": due, "estimated_minutes": 180}
    u_normal = priority._deadline_urgency(normal, FIXED_NOW, 180, 3)
    u_big = priority._deadline_urgency(big, FIXED_NOW, 180, 3)
    # big task's effective deadline is 3 days sooner -> more urgent
    assert u_big > u_normal
    assert u_big == pytest.approx(priority._clip(1.0 - (48.0 / 168.0)))


# --------------------------------------------------------------------------
# not_started coefficient
# --------------------------------------------------------------------------
def test_not_started_coefficients():
    assert priority._not_started({"status": "todo"}) == 1.0
    assert priority._not_started({"status": "in_progress"}) == 0.5
    assert priority._not_started({"status": "weird"}) == 0.3   # kept > 0


# --------------------------------------------------------------------------
# natural-language reason (UI never shows the numeric score)
# --------------------------------------------------------------------------
def test_reason_deadline_dominant(clock):
    a = {"max_points": 10, "category": "homework",
         "due_at": db.to_utc_iso(FIXED_NOW + timedelta(hours=3))}
    txt = priority._reason(0.1, 1.0, 0.1, (0.5, 0.35, 0.15), a)
    assert "締切が近い" in txt


def test_reason_grade_dominant(clock):
    a = {"max_points": 80, "category": "exam",
         "due_at": db.to_utc_iso(FIXED_NOW + timedelta(days=10))}
    txt = priority._reason(1.0, 0.1, 0.1, (0.5, 0.35, 0.15), a)
    assert "配点80" in txt and "試験" in txt


# --------------------------------------------------------------------------
# build_today — overdue isolation, pinned-first, focus, review count
# --------------------------------------------------------------------------
def test_build_today_isolates_overdue(clock):
    co = seed.make_course("数学", "stem")
    seed.make_assignment(co, "遅れ", db.to_utc_iso(FIXED_NOW - timedelta(days=2)))
    seed.make_assignment(co, "未来", db.to_utc_iso(FIXED_NOW + timedelta(days=2)))
    out = priority.build_today()
    todo_titles = {a["title"] for a in out["todo"]}
    overdue_titles = {a["title"] for a in out["overdue"]}
    assert overdue_titles == {"遅れ"}
    assert "遅れ" not in todo_titles           # overdue never squats in the ranking
    assert out["focus"]["title"] == "未来"


def test_build_today_pinned_beats_score(clock):
    co = seed.make_course("英語", "lang")
    # high-scoring, near deadline, big points
    seed.make_assignment(co, "重要そう",
                         db.to_utc_iso(FIXED_NOW + timedelta(hours=2)),
                         category="exam", max_points=100)
    # low-scoring but pinned -> must become focus
    seed.make_assignment(co, "固定",
                         db.to_utc_iso(FIXED_NOW + timedelta(days=6)),
                         category="homework", max_points=1, pinned=1)
    out = priority.build_today()
    assert out["focus"]["title"] == "固定"
    assert out["todo"][0]["title"] == "固定"


def test_build_today_review_due_counts_new_and_due(clock):
    co = seed.make_course()
    seed.make_card(co, "n", "x", state="new")
    seed.make_card(co, "d", "x", state="review",
                   next_due_at=db.to_utc_iso(FIXED_NOW - timedelta(minutes=1)))
    seed.make_card(co, "susp", "x", state="suspended",
                   next_due_at=db.to_utc_iso(FIXED_NOW - timedelta(days=1)))
    out = priority.build_today()
    assert out["review_due"] == 2             # new + due, suspended excluded


def test_build_today_empty(clock):
    out = priority.build_today()
    assert out["focus"] is None and out["todo"] == [] and out["overdue"] == []
    assert out["review_due"] == 0
