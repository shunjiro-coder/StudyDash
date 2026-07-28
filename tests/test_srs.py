"""Pure-logic tests for srs.py (SM-2), clock frozen via the `clock` fixture."""

from datetime import timedelta

import pytest

import db
import srs
from tests import seed
from tests.conftest import FIXED_NOW


# --------------------------------------------------------------------------
# EF update + lapse
# --------------------------------------------------------------------------
def test_ef_good_holds_at_2_5():
    # good (q4) on EF 2.5 is a fixed point of the SM-2 formula
    assert srs._ef_update(2.5, 4) == 2.5


def test_ef_easy_raises():
    assert srs._ef_update(2.5, 5) > 2.5


def test_ef_again_lowers_but_clamps_at_1_3():
    assert srs._ef_update(2.5, 1) == pytest.approx(1.96)
    # already near the floor -> clamped, never below 1.3
    assert srs._ef_update(1.3, 1) == 1.3


def test_again_is_a_lapse(clock):
    co = seed.make_course()
    cid = seed.make_card(co, "Q", "A", state="review", repetitions=5,
                         current_interval=40, current_ease=2.5)
    out = srs.answer(cid, "again")
    assert out["repetitions"] == 0          # reset
    assert out["interval_days"] == 1        # next day
    assert out["ease"] == pytest.approx(1.96)  # EF still updated
    row = db.query_one("SELECT * FROM cards WHERE id=?", (cid,))
    assert row["state"] == "review"
    assert row["repetitions"] == 0 and row["current_interval"] == 1


# --------------------------------------------------------------------------
# Interval staircase: n0->1, n1->6, n>=2 -> prev*EF
# --------------------------------------------------------------------------
def test_staircase_first_good(clock):
    co = seed.make_course()
    cid = seed.make_card(co, "Q1", "A1", state="new", repetitions=0)
    out = srs.answer(cid, "good")
    assert out["interval_days"] == 1 and out["repetitions"] == 1
    row = db.query_one("SELECT next_due_at FROM cards WHERE id=?", (cid,))
    assert row["next_due_at"] == db.to_utc_iso(FIXED_NOW + timedelta(days=1))


def test_staircase_second_good_is_6(clock):
    co = seed.make_course()
    cid = seed.make_card(co, "Q2", "A2", state="review", repetitions=1,
                         current_interval=1)
    out = srs.answer(cid, "good")
    assert out["interval_days"] == 6 and out["repetitions"] == 2


def test_staircase_third_good_is_prev_times_ef(clock):
    co = seed.make_course()
    cid = seed.make_card(co, "Q3", "A3", state="review", repetitions=2,
                         current_interval=6, current_ease=2.5)
    out = srs.answer(cid, "good")     # ef stays 2.5 -> round(6*2.5)=15
    assert out["interval_days"] == 15 and out["repetitions"] == 3


def test_answer_is_atomic_card_plus_review(clock):
    """C1 regression: one grade writes exactly one reviews row AND advances the
    card — both, together."""
    co = seed.make_course()
    cid = seed.make_card(co, "QA", "AA", state="new")
    srs.answer(cid, "good")
    reviews = db.query("SELECT * FROM reviews WHERE card_id=?", (cid,))
    assert len(reviews) == 1
    assert reviews[0]["grade"] == "good"


def test_answer_bad_grade_rejected():
    co = seed.make_course()
    cid = seed.make_card(co, "QB", "AB")
    with pytest.raises(ValueError):
        srs.answer(cid, "nope")


# --------------------------------------------------------------------------
# Queue predicate: (new OR next_due<=now) AND NOT suspended
# --------------------------------------------------------------------------
def test_queue_includes_due_and_new_excludes_suspended(clock):
    co = seed.make_course()
    past = db.to_utc_iso(FIXED_NOW - timedelta(days=1))
    future = db.to_utc_iso(FIXED_NOW + timedelta(days=1))
    due = seed.make_card(co, "due", "x", state="review", next_due_at=past)
    new = seed.make_card(co, "new", "x", state="new")
    seed.make_card(co, "future", "x", state="review", next_due_at=future)
    # suspended card with a PAST due date must not leak out
    seed.make_card(co, "susp", "x", state="suspended", next_due_at=past)

    q = srs.get_queue()
    ids = {c["id"] for c in q["cards"]}
    assert due in ids and new in ids
    assert q["due_count"] == 1 and q["new_count"] == 1
    # counts() mirrors the same predicate
    assert srs.counts() == 2


def test_counts_empty_deck(clock):
    assert srs.counts() == 0
    assert srs.get_queue()["cards"] == []


def test_review_card_with_null_due_is_not_due(clock):
    co = seed.make_course()
    seed.make_card(co, "orphan", "x", state="review", next_due_at=None)
    assert srs.counts() == 0
    assert srs.get_queue()["due_count"] == 0


# --------------------------------------------------------------------------
# _why_now
# --------------------------------------------------------------------------
def test_why_now_new(clock):
    co = seed.make_course()
    cid = seed.make_card(co, "wn", "x", state="new")
    row = db.query_one(srs.CARD_JOIN + " WHERE c.id=?", (cid,))
    assert srs._why_now(row, set()) == "新規カード"


def test_why_now_last_again(clock):
    co = seed.make_course()
    cid = seed.make_card(co, "wa", "x", state="review",
                         next_due_at=db.to_utc_iso(FIXED_NOW))
    seed.add_review(cid, "again")
    row = db.query_one(srs.CARD_JOIN + " WHERE c.id=?", (cid,))
    assert srs._why_now(row, set()) == "前回×"


def test_why_now_exam_range(clock):
    co = seed.make_course()
    cid = seed.make_card(co, "we", "x", state="review",
                         next_due_at=db.to_utc_iso(FIXED_NOW))
    row = db.query_one(srs.CARD_JOIN + " WHERE c.id=?", (cid,))
    assert srs._why_now(row, {co}) == "テスト範囲"


# --------------------------------------------------------------------------
# weak = cumulative again >= 2 (HAVING COUNT>=2), or verified='wrong'
# --------------------------------------------------------------------------
def test_weak_needs_two_agains_cumulative(clock):
    co = seed.make_course()
    one = seed.make_card(co, "one-again", "x")
    seed.add_review(one, "again")           # only one -> not weak
    two = seed.make_card(co, "two-again", "x")
    seed.add_review(two, "again")
    seed.add_review(two, "good")            # non-consecutive, still 2 total agains
    seed.add_review(two, "again")
    wrong = seed.make_card(co, "flagged", "x", verified="wrong")

    ids = {c["id"] for c in srs.weak_cards()}
    assert two in ids and wrong in ids
    assert one not in ids


def test_weak_excludes_suspended(clock):
    co = seed.make_course()
    s = seed.make_card(co, "susp-wrong", "x", state="suspended", verified="wrong")
    assert s not in {c["id"] for c in srs.weak_cards()}


# --------------------------------------------------------------------------
# cram day-split (i % days) — front-loaded, stateless
# --------------------------------------------------------------------------
def test_cram_splits_across_days(clock):
    co = seed.make_course()
    for i in range(10):
        seed.make_card(co, f"c{i}", "x")
    exam = db.to_utc_iso(FIXED_NOW + timedelta(days=5))
    plan = srs.cram_plan(exam, course_id=co)
    assert plan["days_left"] == 5
    assert plan["total_cards"] == 10
    assert sum(plan["per_day_counts"]) == 10
    assert plan["per_day_counts"][0] == 2      # 10 cards / 5 days
    assert len(plan["today"]) == 2


def test_cram_date_only_string_is_naive_safe(clock):
    co = seed.make_course()
    seed.make_card(co, "cd", "x")
    plan = srs.cram_plan("2026-08-01")         # date-only -> naive parse
    assert plan["days_left"] == (
        db.parse_iso("2026-08-01").date() - FIXED_NOW.date()).days
    assert plan["total_cards"] == 1


def test_cram_excludes_suspended(clock):
    co = seed.make_course()
    seed.make_card(co, "act", "x")
    seed.make_card(co, "sus", "x", state="suspended")
    plan = srs.cram_plan(db.to_utc_iso(FIXED_NOW + timedelta(days=3)),
                         course_id=co)
    assert plan["total_cards"] == 1


# --------------------------------------------------------------------------
# redistribute — day-spread overdue, boundaries
# --------------------------------------------------------------------------
def test_redistribute_spreads_overdue(clock):
    co = seed.make_course()
    past = db.to_utc_iso(FIXED_NOW - timedelta(days=30))
    ids = [seed.make_card(co, f"o{i}", "x", state="review", next_due_at=past)
           for i in range(9)]
    n = srs.redistribute(3)
    assert n == 9
    for i, cid in enumerate(ids):
        due = db.query_one("SELECT next_due_at FROM cards WHERE id=?", (cid,))
        expect = db.to_utc_iso(FIXED_NOW + timedelta(days=i % 3))
        assert due["next_due_at"] == expect


def test_redistribute_zero_days_guarded(clock):
    co = seed.make_course()
    past = db.to_utc_iso(FIXED_NOW - timedelta(days=5))
    cid = seed.make_card(co, "z", "x", state="review", next_due_at=past)
    # days=0 must not divide-by-zero; behaves like days=1 (all -> today)
    assert srs.redistribute(0) == 1
    due = db.query_one("SELECT next_due_at FROM cards WHERE id=?", (cid,))
    assert due["next_due_at"] == db.to_utc_iso(FIXED_NOW)


def test_redistribute_empty(clock):
    assert srs.redistribute() == 0


# --------------------------------------------------------------------------
# mastery
# --------------------------------------------------------------------------
def test_mastery_counts_reps_ge_2(clock):
    co = seed.make_course()
    seed.make_card(co, "m0", "x", repetitions=0)
    seed.make_card(co, "m2", "x", repetitions=2)
    seed.make_card(co, "m5", "x", repetitions=5)
    m = srs.mastery(co)
    assert m["total"] == 3 and m["mastered"] == 2 and m["pct"] == 67


def test_mastery_empty(clock):
    assert srs.mastery() == {"pct": 0, "total": 0, "mastered": 0}
