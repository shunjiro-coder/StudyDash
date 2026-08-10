"""G3: session review strategies. A layer ON TOP of SM-2 — a strategy only
reorders the queue or changes how the question is asked, so nothing here may
touch scheduling, card state or the D-5 identity."""

import pytest

import app as app_module
import db
import srs
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


# -------------------- strategy parsing --------------------
def test_clean_strategies_accepts_lists_strings_and_junk():
    assert srs.clean_strategies(["interleave", "retrieval"]) == ["interleave", "retrieval"]
    assert srs.clean_strategies("interleave,elaborate") == ["interleave", "elaborate"]
    assert srs.clean_strategies("  RECOGNITION  ") == ["recognition"]
    assert srs.clean_strategies(["interleave", "interleave"]) == ["interleave"]   # deduped
    assert srs.clean_strategies(["nope", 5, None]) == []
    assert srs.clean_strategies(None) == []


# -------------------- interleaving --------------------
def _c(cid, mid):
    return {"id": cid, "material_id": mid}


def test_interleave_alternates_materials():
    cards = [_c(1, 7), _c(2, 7), _c(3, 7), _c(4, 9), _c(5, 9)]
    out = srs.interleave(cards)
    assert [x["id"] for x in out] == [1, 4, 2, 5, 3]
    # every card survives exactly once — a strategy must never drop study material
    assert sorted(x["id"] for x in out) == [1, 2, 3, 4, 5]


def test_interleave_preserves_order_within_a_material():
    cards = [_c(1, 7), _c(2, 7), _c(3, 8)]
    out = srs.interleave(cards)
    ids7 = [x["id"] for x in out if x["material_id"] == 7]
    assert ids7 == [1, 2]          # SM-2's due-first order survives within a topic


def test_interleave_handles_single_group_null_material_and_empty():
    assert srs.interleave([]) == []
    one = [_c(1, 7), _c(2, 7)]
    assert [x["id"] for x in srs.interleave(one)] == [1, 2]      # no-op, not a crash
    mixed = [_c(1, None), _c(2, 7), _c(3, None)]
    out = srs.interleave(mixed)
    assert sorted(x["id"] for x in out) == [1, 2, 3]             # NULL is its own group


def test_interleave_is_deterministic():
    cards = [_c(i, i % 3) for i in range(9)]
    assert srs.interleave(cards) == srs.interleave(cards)        # no RNG: no reshuffle


# -------------------- recognition distractors --------------------
def test_distractors_come_from_other_cards_in_the_course():
    co = seed.make_course()
    cid = seed.make_card(co, "RNAの糖は？", "リボース")
    seed.make_card(co, "DNAの糖は？", "デオキシリボース")
    seed.make_card(co, "塩基は？", "ウラシル")
    ds = srs.distractors_for(cid, 3)
    assert "リボース" not in ds                     # never the real answer
    assert set(ds) <= {"デオキシリボース", "ウラシル"}


def test_distractors_exclude_other_courses_and_suspended_cards():
    co, other = seed.make_course(), seed.make_course(name="別コース")
    cid = seed.make_card(co, "Q", "本物")
    seed.make_card(other, "Q2", "他コースの答え")
    seed.make_card(co, "Q3", "停止中の答え", state="suspended")
    assert srs.distractors_for(cid, 3) == []


def test_distractors_dedupe_identical_answers():
    co = seed.make_course()
    cid = seed.make_card(co, "Q", "本物")
    seed.make_card(co, "A", "同じ答え")
    seed.make_card(co, "B", "同じ答え ")        # whitespace-different duplicate
    assert srs.distractors_for(cid, 3) == ["同じ答え"]


def test_distractors_are_deterministic_and_respect_limit():
    co = seed.make_course()
    cid = seed.make_card(co, "Q", "1234")
    for i in range(6):
        seed.make_card(co, f"Q{i}", "x" * (i + 1))
    first = srs.distractors_for(cid, 3)
    assert first == srs.distractors_for(cid, 3) and len(first) == 3


def test_distractors_for_missing_card_is_empty():
    assert srs.distractors_for(999999) == []


# -------------------- endpoints --------------------
def test_queue_endpoint_interleaves_and_keeps_its_contract(client):
    co = seed.make_course()
    m1, m2 = seed.make_material(co), seed.make_material(co)
    for i in range(2):
        seed.make_card(co, f"a{i}", "1", material_id=m1, state="new")
        seed.make_card(co, f"b{i}", "2", material_id=m2, state="new")
    body = client.get("/api/review/queue?strategy=interleave").get_json()
    assert {"cards", "new_count", "due_count"} <= set(body)      # contract unchanged
    assert body["strategies"] == ["interleave"]
    mats = [c["material_id"] for c in body["cards"]]
    assert mats[0] != mats[1]                                    # actually alternating


def test_queue_endpoint_ignores_unknown_strategies(client):
    body = client.get("/api/review/queue?strategy=nonsense").get_json()
    assert body["strategies"] == []


def test_distractors_endpoint(client):
    co = seed.make_course()
    cid = seed.make_card(co, "Q", "本物")
    seed.make_card(co, "Q2", "偽物")
    body = client.get(f"/api/cards/{cid}/distractors?limit=2").get_json()
    assert body["distractors"] == ["偽物"]
    assert client.get("/api/cards/999999/distractors").status_code == 404


def test_by_material_endpoint_accepts_interleave(client):
    co = seed.make_course()
    m1, m2 = seed.make_material(co), seed.make_material(co)
    seed.make_card(co, "a", "1", material_id=m1, state="new")
    seed.make_card(co, "b", "2", material_id=m2, state="new")
    res = client.get(f"/api/review/by-material?material_id={m1}&material_id={m2}&strategy=interleave")
    assert res.status_code == 200 and len(res.get_json()) == 2


def test_strategies_never_touch_scheduling_or_identity():
    """The whole premise of G3: it is a presentation layer."""
    co = seed.make_course()
    mid = seed.make_material(co)
    cid = seed.make_card(co, "Q", "A", material_id=mid, state="new")
    before = dict(db.query_one(
        "SELECT content_hash, state, next_due_at, current_interval, current_ease, repetitions "
        "FROM cards WHERE id=?", (cid,)))
    srs.interleave([{"id": cid, "material_id": mid}])
    srs.distractors_for(cid)
    after = dict(db.query_one(
        "SELECT content_hash, state, next_due_at, current_interval, current_ease, repetitions "
        "FROM cards WHERE id=?", (cid,)))
    assert after == before
