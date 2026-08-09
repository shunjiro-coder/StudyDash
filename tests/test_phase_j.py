"""Phase J: card translation (ja<->en flip), review-by-material (pick + merge),
notes search, and per-generation language threading. The single AI call each of
these makes is mocked via generate._generate_with_retry."""

import json

import pytest

import app as app_module
import db
import generate
import srs
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


# -------------------- card translation (#4) --------------------
def test_translation_column_exists():
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(cards)")}
    assert "translation_json" in cols


def test_translate_card_caches_and_leaves_hash_untouched(monkeypatch):
    co = seed.make_course()
    cid = seed.make_card(co, "光合成とは？", "光で有機物を作る過程")
    before = db.query_one("SELECT content_hash FROM cards WHERE id=?", (cid,))["content_hash"]
    calls = []
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: (
        calls.append(1) or {"front": "What is photosynthesis?", "back": "Making organics with light"}))
    r1 = generate.translate_card(cid, "en")
    assert r1["front"].startswith("What") and r1["cached"] is False
    r2 = generate.translate_card(cid, "en")            # served from cache
    assert r2["cached"] is True and len(calls) == 1     # no second AI call
    after = db.query_one("SELECT content_hash, translation_json FROM cards WHERE id=?", (cid,))
    assert after["content_hash"] == before             # D-5 identity untouched
    assert json.loads(after["translation_json"])["en"]["front"].startswith("What")


def test_translate_bad_lang_raises():
    co = seed.make_course()
    cid = seed.make_card(co, "Q", "A")
    with pytest.raises(ValueError):
        generate.translate_card(cid, "fr")


def test_translate_endpoint_ok(client, monkeypatch):
    co = seed.make_course()
    cid = seed.make_card(co, "細胞とは", "生命の基本単位")
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"front": "cell", "back": "unit of life"})
    body = client.post(f"/api/cards/{cid}/translate", json={"lang": "en"}).get_json()
    assert body["ok"] and body["translation"]["front"] == "cell"


def test_translate_endpoint_bad_lang_ok_false(client):
    co = seed.make_course()
    cid = seed.make_card(co, "Q", "A")
    res = client.post(f"/api/cards/{cid}/translate", json={"lang": "zz"})
    assert res.status_code == 200 and res.get_json()["ok"] is False


def test_card_dict_exposes_translation(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co)
    cid = seed.make_card(co, "Q", "A", material_id=mid)
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"front": "Qx", "back": "Ax"})
    generate.translate_card(cid, "en")
    card = client.get(f"/api/materials/{mid}").get_json()["cards"][0]
    assert card["translation"]["en"]["front"] == "Qx"


# -------------------- review by material (#5) --------------------
def test_review_materials_groups_counts_and_label():
    co = seed.make_course()
    m1 = seed.make_material(co, extracted_json={"summary": "光合成の教材"})
    m2 = seed.make_material(co)
    seed.make_card(co, "a", "1", material_id=m1, state="new")
    seed.make_card(co, "b", "2", material_id=m1, state="new")
    seed.make_card(co, "c", "3", material_id=m2, state="new")
    seed.make_card(co, "d", "4", material_id=m1, state="suspended")   # excluded
    seed.make_card(co, "e", "5", state="new")                        # material-less bucket
    by = {x["material_id"]: x for x in srs.review_materials()}
    assert by[m1]["total"] == 2 and by[m1]["new_count"] == 2
    assert by[m1]["label"] == "光合成の教材"
    assert None in by and by[None]["total"] == 1                     # 教材なし bucket


def test_by_materials_merge_and_null_bucket():
    co = seed.make_course()
    m1, m2 = seed.make_material(co), seed.make_material(co)
    c1 = seed.make_card(co, "a", "1", material_id=m1, state="new")
    c2 = seed.make_card(co, "b", "2", material_id=m2, state="new")
    c3 = seed.make_card(co, "c", "3", state="new")
    seed.make_card(co, "d", "4", material_id=m1, state="proposed")   # excluded
    assert {c["id"] for c in srs.by_materials([str(m1), str(m2)])} == {c1, c2}
    assert {c["id"] for c in srs.by_materials(["none"])} == {c3}
    assert srs.by_materials([]) == []                                # no tokens -> empty


def test_by_materials_tolerates_crafted_tokens():
    # a public endpoint must not 500 on a non-ASCII "digit" that passes isdigit()
    # but raises on int() (e.g. superscript '²'), nor on plain junk.
    assert srs.by_materials(["²"]) == []
    assert srs.by_materials(["abc", "-1", "0"]) == []


def test_by_material_endpoint(client):
    co = seed.make_course()
    m1 = seed.make_material(co)
    seed.make_card(co, "a", "1", material_id=m1, state="new")
    res = client.get(f"/api/review/by-material?material_id={m1}")
    assert res.status_code == 200 and len(res.get_json()) == 1


# -------------------- notes search (#9) --------------------
def test_search_matches_title_and_rem_text(client):
    d1 = seed.make_doc(title="生物のノート")
    seed.make_rem(d1, "光合成について")
    d2 = seed.make_doc(title="数学")
    seed.make_rem(d2, "二次方程式")
    docs = client.get("/api/search?q=光合成").get_json()["docs"]
    assert len(docs) == 1 and docs[0]["id"] == d1
    assert "光合成" in (docs[0]["snippet"] or "")
    assert client.get("/api/search?q=数学").get_json()["docs"][0]["id"] == d2   # title match


def test_search_empty_query_returns_nothing(client):
    assert client.get("/api/search?q=").get_json()["docs"] == []


def test_search_wildcards_are_literal(client):
    d = seed.make_doc(title="割引50%オフ")
    seed.make_rem(d, "本文")
    assert len(client.get("/api/search?q=50%25").get_json()["docs"]) == 1   # %25 -> literal '50%'
    assert client.get("/api/search?q=%25%25%25").get_json()["docs"] == []   # '%%%' must NOT match everything


# -------------------- language threading (#7) --------------------
def test_quiz_endpoint_threads_explicit_lang(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="content " * 40)
    seen = {}
    def fake(prompt, model):
        seen["prompt"] = prompt
        return {"questions": [{"type": "written", "question": "Q", "answer": "A"}]}
    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    assert client.post(f"/api/materials/{mid}/quiz",
                       json={"format": "written", "lang": "en"}).get_json()["ok"]
    assert "English" in seen["prompt"]        # the 'en' directive reached the prompt


def test_summary_endpoint_threads_explicit_lang(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="内容 " * 40)
    seen = {}
    def fake(prompt, model):
        seen["prompt"] = prompt
        return {"summary_md": "# ok"}
    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    assert client.post(f"/api/materials/{mid}/summary",
                       json={"lang": "en"}).get_json()["ok"]
    assert "English" in seen["prompt"]
