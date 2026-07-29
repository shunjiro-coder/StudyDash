"""Phase H0: modality foundation — cards.media_json (per-modality render
structure) is additive, exposed to the frontend via card_type + media_json, and
EXCLUDED from the D-5 content_hash so richer types never disturb dedup."""

import json

import app as app_module
import db
import srs
from tests import seed


def test_media_json_column_added_by_migration():
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(cards)")}
    assert "media_json" in cols


def test_queue_exposes_card_type_and_media_json():
    co = seed.make_course()
    mid = seed.make_material(co)
    cid = seed.make_card(co, "解き方の手順は？", "着眼点", state="new",
                         material_id=mid, card_type="steps")
    db.write("UPDATE cards SET media_json=? WHERE id=?",
             (json.dumps({"steps": ["因数分解", "解の公式"]}), cid))
    card = next(c for c in srs.get_queue()["cards"] if c["id"] == cid)
    assert card["card_type"] == "steps"
    assert card["media_json"]["steps"] == ["因数分解", "解の公式"]


def test_card_dict_exposes_media_json():
    co = seed.make_course()
    cid = seed.make_card(co, "光合成は___で有機物を作る", "光", card_type="cloze")
    db.write("UPDATE cards SET media_json=? WHERE id=?",
             (json.dumps({"rubric": "観点"}), cid))
    d = app_module.card_dict(db.query_one("SELECT * FROM cards WHERE id=?", (cid,)))
    assert d["card_type"] == "cloze" and d["media_json"] == {"rubric": "観点"}


def test_media_json_excluded_from_content_hash():
    # identity is front/back only — media_json must not enter the hash
    assert db.content_hash("同じ問い", "同じ答え") == db.content_hash("同じ問い", "同じ答え")
    co = seed.make_course()
    cid = seed.make_card(co, "同じ問い", "同じ答え", card_type="qa")
    row = db.query_one("SELECT content_hash FROM cards WHERE id=?", (cid,))
    assert row["content_hash"] == db.content_hash("同じ問い", "同じ答え")
