"""Phase O: FSRS-4.5 as an optional scheduler.

Two things are load-bearing. The algorithm has to match the published one (tested
as pure arithmetic, no DB), and turning it on must not disturb a deck that has
been scheduled by SM-2 — including switching back.
"""

import json

import pytest

import app as app_module
import db
import fsrs
import srs
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


@pytest.fixture
def use_fsrs():
    db.save_settings({"scheduler": "fsrs"})
    yield
    db.save_settings({"scheduler": "sm2"})


# -------------------- the algorithm, as pure arithmetic --------------------
def test_retrievability_is_the_target_at_t_equals_stability():
    """FACTOR is defined so that a card is at exactly 90% when elapsed == S."""
    assert round(fsrs.retrievability(10, 10), 6) == 0.9
    assert round(fsrs.retrievability(0.5, 0.5), 6) == 0.9


def test_retrievability_falls_monotonically():
    prev = 1.0
    for t in (0, 1, 5, 20, 100, 1000):
        r = fsrs.retrievability(t, 10)
        assert r <= prev
        prev = r
    assert fsrs.retrievability(0, 10) == 1.0


def test_initial_values_are_ordered_by_button():
    """A harder button must mean lower stability and higher difficulty. The
    exponential FSRS-5 formula with these 4.5 weights collapsed good AND easy to
    difficulty 1.0, which this ordering assertion catches."""
    stabilities = [fsrs.initial_stability(g) for g in (1, 2, 3, 4)]
    difficulties = [fsrs.initial_difficulty(g) for g in (1, 2, 3, 4)]
    assert stabilities == sorted(stabilities), "stability must rise with the grade"
    assert difficulties == sorted(difficulties, reverse=True), \
        "difficulty must fall as the grade improves"
    assert len(set(difficulties)) == 4, "buttons must be distinguishable"
    assert 4.0 < fsrs.initial_difficulty(3) < 6.5    # 'good' lands mid-scale


def test_difficulty_stays_in_range_under_abuse():
    d = fsrs.initial_difficulty(3)
    for _ in range(50):
        d = fsrs.next_difficulty(d, 1)
    assert d <= fsrs.MAX_DIFFICULTY
    for _ in range(50):
        d = fsrs.next_difficulty(d, 4)
    assert d >= fsrs.MIN_DIFFICULTY


def test_repeated_success_grows_the_interval():
    s = d = None
    intervals, elapsed = [], 0
    for _ in range(5):
        s, d, elapsed = fsrs.review(s, d, elapsed, 3)
        intervals.append(elapsed)
    assert intervals == sorted(intervals)
    assert intervals[-1] > intervals[0] * 10


def test_a_lapse_shortens_the_interval_and_hardens_the_card():
    s = d = None
    iv = 0
    for _ in range(3):
        s, d, iv = fsrs.review(s, d, iv, 3)
    before_s, before_d, before_iv = s, d, iv
    s, d, iv = fsrs.review(s, d, iv, 1)
    assert s < before_s, "stability must drop on a lapse"
    assert d > before_d, "difficulty must rise on a lapse"
    assert iv < before_iv


def test_lapse_never_increases_stability():
    """Guard on the published lapse formula, which can exceed S for a small S."""
    for s0 in (0.1, 0.5, 1.0, 3.0):
        s, _ = fsrs.next_state(s0, 5.0, 1, 1)
        assert s <= s0


def test_interval_is_at_least_one_day():
    """There are no intra-day learning steps here, so nothing returns today."""
    assert fsrs.interval_for(0.01) == 1
    assert fsrs.interval_for(fsrs.MIN_STABILITY) >= 1


def test_higher_retention_means_shorter_intervals():
    assert fsrs.interval_for(50, 0.95) < fsrs.interval_for(50, 0.80)


def test_bad_input_is_rejected_not_guessed():
    with pytest.raises(ValueError):
        fsrs.next_state(1.0, 5.0, 1, 0)
    with pytest.raises(ValueError):
        fsrs.next_state(1.0, 5.0, 1, 5)
    with pytest.raises(ValueError):
        fsrs.interval_for(10, 1.5)


# -------------------- wiring --------------------
def test_sm2_is_the_default(clock):
    assert srs.scheduler_name() == "sm2"


def test_grade_scales_are_distinct():
    """QUALITY is SM-2's 0-5 scale, FSRS_GRADE is FSRS's 1-4. Sharing button
    labels does not make them the same number."""
    assert srs.QUALITY != srs.FSRS_GRADE
    assert srs.QUALITY["good"] == 4 and srs.FSRS_GRADE["good"] == 3
    assert set(srs.FSRS_GRADE.values()) == {1, 2, 3, 4}


def test_fsrs_answer_writes_state_and_schedules(clock, use_fsrs):
    co = seed.make_course()
    cid = seed.make_card(co, "q", "a", state="new")
    out = srs.answer(cid, "good")
    assert out["scheduler"] == "fsrs"
    assert out["interval_days"] >= 1
    row = db.query_one("SELECT * FROM cards WHERE id=?", (cid,))
    state = json.loads(row["fsrs_json"])
    assert state["s"] > 0 and 1 <= state["d"] <= 10
    assert row["state"] == "review"
    # the immutable review row is still appended, same as SM-2
    assert db.query_one("SELECT COUNT(*) n FROM reviews WHERE card_id=?",
                        (cid,))["n"] == 1


def test_fsrs_does_not_touch_sm2_ease(clock, use_fsrs):
    """Switching back to SM-2 must resume where SM-2 left off."""
    co = seed.make_course()
    cid = seed.make_card(co, "q2", "a2", state="new")
    before = db.query_one("SELECT current_ease FROM cards WHERE id=?",
                          (cid,))["current_ease"]
    srs.answer(cid, "again")
    after = db.query_one("SELECT current_ease FROM cards WHERE id=?",
                         (cid,))["current_ease"]
    assert after == before


def test_sm2_path_is_unchanged_when_fsrs_is_off(clock):
    """The whole point of opt-in: default behaviour is bit-for-bit as before."""
    co = seed.make_course()
    cid = seed.make_card(co, "q3", "a3", state="new")
    out = srs.answer(cid, "good")
    assert "scheduler" not in out          # SM-2's response shape is untouched
    assert out["interval_days"] == 1       # SM-2's first-review interval
    # q=4 is the neutral grade in SM-2: 2.5 + (0.1 - 1*(0.08 + 0.02)) == 2.5.
    # Only 'easy' (q=5) moves the ease factor up.
    assert out["ease"] == 2.5
    easy = srs.answer(seed.make_card(co, "q3b", "a3b", state="new"), "easy")
    assert easy["ease"] > 2.5
    assert db.query_one("SELECT fsrs_json FROM cards WHERE id=?",
                        (cid,))["fsrs_json"] is None


def test_corrupt_fsrs_state_is_treated_as_a_first_review(clock, use_fsrs):
    co = seed.make_course()
    cid = seed.make_card(co, "q4", "a4", state="new")
    db.write("UPDATE cards SET fsrs_json=? WHERE id=?", ("{not json", cid))
    out = srs.answer(cid, "good")          # must not raise
    assert out["interval_days"] >= 1


def test_pacing_and_identity_are_untouched_by_scheduling(clock, use_fsrs):
    co = seed.make_course()
    cid = seed.make_card(co, "q5", "a5", state="new")
    before = db.query_one("SELECT content_hash, front, back FROM cards WHERE id=?",
                          (cid,))
    srs.answer(cid, "hard")
    after = db.query_one("SELECT content_hash, front, back FROM cards WHERE id=?",
                         (cid,))
    assert dict(after) == dict(before)     # D-5 hash and content unchanged


def test_retention_setting_is_clamped(clock):
    db.save_settings({"fsrs_retention": 0.999})
    assert srs.target_retention() <= 0.97
    db.save_settings({"fsrs_retention": 0.1})
    assert srs.target_retention() >= 0.70
    db.save_settings({"fsrs_retention": "nonsense"})
    assert srs.target_retention() == srs.DEFAULT_RETENTION


# -------------------- endpoint --------------------
def test_settings_endpoint_switches_scheduler(client, clock):
    body = client.patch("/api/settings", json={"scheduler": "fsrs"}).get_json()
    assert body["scheduler"] == "fsrs"
    assert client.get("/api/settings").get_json()["scheduler"] == "fsrs"
    client.patch("/api/settings", json={"scheduler": "sm2"})
    assert srs.scheduler_name() == "sm2"


def test_settings_endpoint_rejects_junk(client, clock):
    assert client.patch("/api/settings", json={"scheduler": "anki"}).status_code == 400
    assert client.patch("/api/settings",
                        json={"fsrs_retention": 2}).status_code == 400
    assert client.patch("/api/settings",
                        json={"fsrs_retention": "x"}).status_code == 400
