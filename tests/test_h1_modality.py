"""Phase H1: the generator stamps the best modality (card_type) + render structure
(media_json: steps/items/rubric) per item. media_json is validated on the way in
and EXCLUDED from the D-5 content_hash, so richer modalities never disturb dedup."""

import json

import db
import generate
from tests import seed


def test_card_type_whitelist():
    assert generate._clean_card_type("steps") == "steps"
    assert generate._clean_card_type("EXPLAIN") == "explain"
    assert generate._clean_card_type("bogus") == "qa"
    assert generate._clean_card_type(None) == "qa"


def test_media_json_kept_per_type():
    assert json.loads(generate._clean_media_json("steps", {"steps": ["a", "b"]})) == {"steps": ["a", "b"]}
    assert json.loads(generate._clean_media_json("compute", {"steps": ["x"]})) == {"steps": ["x"]}
    assert json.loads(generate._clean_media_json("list", {"items": ["x"]})) == {"items": ["x"]}
    assert json.loads(generate._clean_media_json("explain", {"rubric": "観点"})) == {"rubric": "観点"}
    # wrong key for the type, empties, and non-dicts collapse to None
    assert generate._clean_media_json("qa", {"steps": ["a"]}) is None
    assert generate._clean_media_json("steps", {"items": ["a"]}) is None
    assert generate._clean_media_json("explain", "notadict") is None
    assert generate._clean_media_json("list", {"items": []}) is None
    # malformed non-list steps/items must NOT char-explode or raise (defensive)
    assert generate._clean_media_json("steps", {"steps": "因数分解する"}) is None
    assert generate._clean_media_json("list", {"items": "abc"}) is None
    assert generate._clean_media_json("steps", {"steps": 5}) is None


def test_insert_stores_card_type_and_media_json():
    co = seed.make_course()
    mid = seed.make_material(co)
    added = generate._insert_generated_cards(co, mid, [
        {"front": "二次方程式の解き方は？", "back": "因数分解→解の公式", "card_type": "steps",
         "media_json": {"steps": ["因数分解を試す", "解の公式"]}},
        {"front": "細胞小器官を3つ", "back": "核\nミト\n葉緑体", "card_type": "list",
         "media_json": {"items": ["核", "ミトコンドリア", "葉緑体"]}},
        {"front": "光合成を説明せよ", "back": "光で有機物を作る", "card_type": "explain",
         "media_json": {"rubric": "光エネルギー・無機物→有機物 に触れる"}},
        {"front": "壊れた", "back": "", "card_type": "qa"},         # dropped (no back)
    ])
    assert added == 3
    rows = db.query("SELECT card_type, media_json FROM cards WHERE material_id=? ORDER BY id", (mid,))
    assert rows[0]["card_type"] == "steps"
    assert json.loads(rows[0]["media_json"])["steps"] == ["因数分解を試す", "解の公式"]
    assert json.loads(rows[1]["media_json"])["items"][0] == "核"
    assert json.loads(rows[2]["media_json"])["rubric"].startswith("光エネルギー")


def test_media_json_excluded_from_content_hash_and_backfilled():
    co = seed.make_course()
    mid = seed.make_material(co)
    generate._insert_generated_cards(co, mid, [{"front": "同じ問い", "back": "同じ答え", "card_type": "qa"}])
    # re-insert same front/back with media_json -> deduped (not in hash), backfilled
    generate._insert_generated_cards(co, mid, [
        {"front": "同じ問い", "back": "同じ答え", "card_type": "steps", "media_json": {"steps": ["s1"]}}])
    rows = db.query("SELECT media_json FROM cards WHERE material_id=?", (mid,))
    assert len(rows) == 1                                   # no duplicate row
    assert json.loads(rows[0]["media_json"])["steps"] == ["s1"]   # backfilled onto existing


def test_backfill_does_not_overwrite_existing_media_json():
    co = seed.make_course()
    mid = seed.make_material(co)
    generate._insert_generated_cards(co, mid, [
        {"front": "Q", "back": "A", "card_type": "steps", "media_json": {"steps": ["first"]}}])
    generate._insert_generated_cards(co, mid, [
        {"front": "Q", "back": "A", "card_type": "steps", "media_json": {"steps": ["second"]}}])
    row = db.query_one("SELECT media_json FROM cards WHERE material_id=?", (mid,))
    assert json.loads(row["media_json"])["steps"] == ["first"]     # existing preserved
