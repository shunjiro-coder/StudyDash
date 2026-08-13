"""H5: multiple-choice cards (distractors in media_json, which is EXCLUDED from the
D-5 hash) and bidirectional cards (a mechanical front/back swap — no AI, and the
swap necessarily hashes differently, so the mirror is its own card)."""

import json

import pytest

import app as app_module
import db
import generate
import methods
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


# -------------------- 多肢選択 --------------------
def test_choice_is_a_valid_card_type_and_has_a_method():
    assert "choice" in generate.VALID_CARD_TYPES
    assert methods.get_method("choice")["card_type"] == "choice"


def test_clean_media_json_keeps_choices_and_drops_the_answer():
    mj = {"choices": ["誤答A", "正解", "誤答B", "誤答A", "", None]}
    out = json.loads(generate._clean_media_json("choice", mj, "正解"))
    assert out["choices"] == ["誤答A", "誤答B"]     # answer, dup and empties dropped


def test_numeric_choices_are_kept_as_strings():
    """A maths card's distractors are legitimately numbers ("2+3?" -> 4, 5, 6)."""
    out = json.loads(generate._clean_media_json("choice", {"choices": [4, 6]}, "5"))
    assert out["choices"] == ["4", "6"]


def test_clean_media_json_caps_choices():
    mj = {"choices": [f"x{i}" for i in range(20)]}
    out = json.loads(generate._clean_media_json("choice", mj, "ans"))
    assert len(out["choices"]) == generate.CHOICE_MAX


def test_choices_are_ignored_for_other_card_types():
    assert generate._clean_media_json("qa", {"choices": ["a", "b"]}, "ans") is None


def test_generated_choice_card_stores_distractors_without_touching_identity():
    co = seed.make_course()
    generate._insert_generated_cards(co, None, [{
        "front": "RNAの糖は？", "back": "リボース", "card_type": "choice",
        "media_json": {"choices": ["デオキシリボース", "リボース", "グルコース"]},
    }])
    row = db.query_one("SELECT * FROM cards WHERE course_id=?", (co,))
    assert row["card_type"] == "choice"
    assert json.loads(row["media_json"])["choices"] == ["デオキシリボース", "グルコース"]
    # D-5: identity is front+back only — the distractors must not enter the hash
    assert row["content_hash"] == db.content_hash("RNAの糖は？", "リボース")


def test_recast_to_choice_carries_distractors(monkeypatch):
    co = seed.make_course()
    cid = seed.make_card(co, "RNAの糖は？", "リボース")
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: {
        "front": "RNAの糖は次のうちどれ？", "back": "リボース",
        "choices": ["デオキシリボース", "グルコース", "スクロース"]})
    new = generate.recast_card(cid, methods.get_method("choice"))
    assert new["card_type"] == "choice"
    assert json.loads(new["media_json"])["choices"] == ["デオキシリボース", "グルコース", "スクロース"]


# -------------------- 双方向 --------------------
def test_reverse_swaps_front_and_back_into_a_new_card():
    co = seed.make_course()
    cid = seed.make_card(co, "光合成", "光で有機物を作る過程", card_type="term")
    new = generate.reverse_card(cid)
    assert new["front"] == "光で有機物を作る過程" and new["back"] == "光合成"
    assert new["state"] == "new" and new["card_type"] == "term"
    # a genuinely different identity — D-5 falls out of the swap for free
    assert new["content_hash"] != db.query_one(
        "SELECT content_hash FROM cards WHERE id=?", (cid,))["content_hash"]


def test_reverse_leaves_the_original_untouched():
    """Unlike a recast, the point is to study BOTH directions."""
    co = seed.make_course()
    cid = seed.make_card(co, "光合成", "光で有機物を作る過程", card_type="term",
                         state="review", repetitions=4, current_interval=21)
    before = dict(db.query_one("SELECT * FROM cards WHERE id=?", (cid,)))
    generate.reverse_card(cid)
    after = dict(db.query_one("SELECT * FROM cards WHERE id=?", (cid,)))
    assert after == before


def test_reverse_inherits_material_and_source_but_starts_fresh_srs():
    co = seed.make_course()
    mid = seed.make_material(co)
    cid = seed.make_card(co, "用語", "定義", card_type="term", material_id=mid,
                         state="review", repetitions=7)
    new = generate.reverse_card(cid)
    assert new["material_id"] == mid
    assert new["repetitions"] == 0 and new["current_ease"] == 2.5


def test_reverse_refuses_a_second_time():
    co = seed.make_course()
    cid = seed.make_card(co, "用語", "定義", card_type="term")
    generate.reverse_card(cid)
    with pytest.raises(ValueError):
        generate.reverse_card(cid)


@pytest.mark.parametrize("ctype", ["steps", "produce", "cloze", "list", "compute"])
def test_reverse_refuses_types_where_it_is_nonsense(ctype):
    co = seed.make_course()
    cid = seed.make_card(co, "問い", "答え", card_type=ctype)
    with pytest.raises(ValueError):
        generate.reverse_card(cid)


def test_reverse_refuses_identical_sides():
    co = seed.make_course()
    cid = seed.make_card(co, "同じ", "同じ", card_type="qa")
    with pytest.raises(ValueError):
        generate.reverse_card(cid)


def test_reverse_of_missing_card_is_none():
    assert generate.reverse_card(999999) is None


# -------------------- endpoint --------------------
def test_reverse_endpoint(client):
    co = seed.make_course()
    cid = seed.make_card(co, "用語", "定義", card_type="term")
    body = client.post(f"/api/cards/{cid}/reverse").get_json()
    assert body["ok"] and body["card"]["front"] == "定義"
    again = client.post(f"/api/cards/{cid}/reverse")
    assert again.status_code == 200 and again.get_json()["ok"] is False   # not a 500
    assert client.post("/api/cards/999999/reverse").status_code == 404


def test_reverse_endpoint_refuses_unreversible_type_with_a_message(client):
    co = seed.make_course()
    cid = seed.make_card(co, "問い", "手順", card_type="steps")
    body = client.post(f"/api/cards/{cid}/reverse").get_json()
    assert body["ok"] is False and "逆向き" in body["message"]


@pytest.mark.parametrize("junk", [None, True, False, {"a": 1}, ["x"]])
def test_non_content_values_are_dropped_not_stringified(junk):
    """str(None) is "None" — which would appear as a selectable answer option."""
    out = generate._clean_media_json("choice", {"choices": [junk, "本物の誤答"]}, "ans")
    assert json.loads(out)["choices"] == ["本物の誤答"]


@pytest.mark.parametrize("junk", [None, True, {"a": 1}])
def test_steps_and_items_drop_junk_too(junk):
    steps = json.loads(generate._clean_media_json("steps", {"steps": [junk, "手順1"]}, ""))
    items = json.loads(generate._clean_media_json("list", {"items": [junk, "項目1"]}, ""))
    assert steps["steps"] == ["手順1"] and items["items"] == ["項目1"]


def test_courseless_reverse_does_not_duplicate():
    """NULL course_id voids the UNIQUE(course_id, content_hash) dedup, so a second
    click used to silently insert a duplicate mirror."""
    cid = seed.make_card(None, "用語", "定義", card_type="term")
    generate.reverse_card(cid)
    with pytest.raises(ValueError):
        generate.reverse_card(cid)
    assert db.query_one("SELECT COUNT(*) n FROM cards")["n"] == 2
