"""Phase K: one canonical term table per material, injected into every prompt about
that material so terminology stops drifting between a card, its summary and its quiz.
The single AI call is mocked via generate._generate_with_retry."""

import json

import pytest

import app as app_module
import db
import generate
import ingest
from tests import seed

TERMS = {"terms": [
    {"src": "nucleotide", "ja": "ヌクレオチド", "en": "nucleotide"},
    {"src": "template", "ja": "鋳型", "en": "template"},
]}


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def _material(**kw):
    kw.setdefault("extracted_text", "nucleotide template " * 60)  # over GLOSSARY_MIN_CHARS
    return seed.make_material(seed.make_course(), **kw)


def test_glossary_column_exists():
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(materials)")}
    assert "glossary_json" in cols


def test_build_glossary_caches_and_reuses(monkeypatch):
    mid = _material()
    calls = []
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: (calls.append(1), TERMS)[1])
    first = generate.build_glossary(mid)
    assert {t["ja"] for t in first} == {"ヌクレオチド", "鋳型"}
    second = generate.build_glossary(mid)          # served from the cached column
    assert second == first and len(calls) == 1     # no second AI call
    stored = db.query_one("SELECT glossary_json FROM materials WHERE id=?", (mid,))
    assert json.loads(stored["glossary_json"])["terms"][0]["ja"] == "ヌクレオチド"


def test_empty_glossary_is_cached_not_rebuilt(monkeypatch):
    """A material whose glossary legitimately comes back empty must not re-run the
    AI call on every single later generation."""
    mid = _material()
    calls = []
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: (calls.append(1), {"terms": []})[1])
    assert generate.build_glossary(mid) == []
    assert generate.build_glossary(mid) == []
    assert generate._glossary_line(mid) == ""
    assert len(calls) == 1                       # built once, then served from cache


def test_build_glossary_force_rebuilds(monkeypatch):
    mid = _material()
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: {"terms": []})
    generate.build_glossary(mid)
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: TERMS)
    assert [t["ja"] for t in generate.build_glossary(mid, force=True)] == ["ヌクレオチド", "鋳型"]


def test_build_glossary_skips_short_material(monkeypatch):
    mid = _material(extracted_text="short")
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: pytest.fail("must not call AI for a tiny material"))
    assert generate.build_glossary(mid) == []


def test_build_glossary_drops_incomplete_and_duplicate_terms(monkeypatch):
    mid = _material()
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: {"terms": [
        {"src": "a", "ja": "ヌクレオチド", "en": "nucleotide"},
        {"src": "b", "ja": "ヌクレオチド", "en": "Nucleotide"},   # dup (case-insensitive)
        {"src": "c", "ja": "", "en": "orphan"},                  # missing ja
        {"src": "d", "ja": "鋳型", "en": ""},                     # missing en
    ]})
    assert [t["ja"] for t in generate.build_glossary(mid)] == ["ヌクレオチド"]


def test_glossary_line_injects_pairs_and_is_empty_without_one(monkeypatch):
    mid = _material()
    assert generate._glossary_line(mid, build=False) == ""      # nothing built yet
    assert generate._glossary_line(None) == ""
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: TERMS)
    generate.build_glossary(mid)
    line = generate._glossary_line(mid, build=False)
    assert "nucleotide=ヌクレオチド" in line and "template=鋳型" in line


def test_glossary_reaches_the_quiz_prompt(monkeypatch):
    mid = _material()
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: TERMS)
    generate.build_glossary(mid)
    seen = {}

    def fake(prompt, model):
        seen["prompt"] = prompt
        return {"questions": [{"type": "written", "question": "Q", "answer": "A"}]}
    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    generate.generate_quiz(mid, fmt="written")
    assert "nucleotide=ヌクレオチド" in seen["prompt"]


def test_glossary_reaches_the_translate_prompt(monkeypatch):
    mid = _material()
    cid = seed.make_card(seed.make_course(), "Q", "A", material_id=mid)
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: TERMS)
    generate.build_glossary(mid)
    seen = {}

    def fake(prompt, model):
        seen["prompt"] = prompt
        return {"front": "F", "back": "B"}
    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    generate.translate_card(cid, "en")
    assert "template=鋳型" in seen["prompt"]


def test_generation_survives_a_glossary_failure(monkeypatch):
    """A glossary is an enhancement: if building one fails, the summary the user
    actually asked for must still be produced."""
    mid = _material()
    calls = []

    def fake(prompt, model):
        calls.append(prompt)
        if "用語集エンジン" in prompt:            # the glossary call
            raise generate.ai.ClaudeError("boom")
        return {"summary_md": "# ok"}
    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    row = generate.generate_summary(mid)
    assert row is not None and len(calls) == 2   # glossary attempted, summary still made


@pytest.mark.parametrize("bad_terms", [
    [{"src": "a", "ja": 3, "en": "nucleotide"}],          # number where a string goes
    [{"src": "a", "ja": ["x"], "en": "nucleotide"}],      # list
    [{"src": None, "ja": "ヌクレオチド", "en": "nucleotide"}],
    "not a list",
    None,
])
def test_build_glossary_survives_non_string_terms(monkeypatch, bad_terms):
    """A model reply with the right shape but wrong value types must never raise —
    it used to escape as AttributeError, 500 the endpoint and strand the material."""
    mid = _material()
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"terms": bad_terms})
    out = generate.build_glossary(mid)              # must not raise
    assert isinstance(out, list)
    assert generate._glossary_line(mid, build=False) == "" or out


def test_shapeless_reply_is_cached_not_retried_forever(monkeypatch):
    mid = _material()
    calls = []
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: (calls.append(1), {"nope": True})[1])
    assert generate.build_glossary(mid) == []
    assert generate.build_glossary(mid) == []
    assert len(calls) == 1                          # cached as "built, no terms"


def test_glossary_line_never_raises(monkeypatch):
    """_glossary_line is called while building someone else's prompt."""
    mid = _material()

    def boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(generate, "build_glossary", boom)
    assert generate._glossary_line(mid) == ""


def test_glossary_reaches_card_generation_and_draft(monkeypatch):
    """The card paths are the primary study surface — they must be glossed too."""
    mid = _material()
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: TERMS)
    generate.build_glossary(mid)
    seen = []

    def fake(prompt, model):
        seen.append(prompt)
        return {"cards": [{"front": "F", "back": "B"}]}
    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    generate.generate_for_material(mid)
    generate.generate_draft(mid)
    assert len(seen) == 2
    assert all("template=鋳型" in p for p in seen)


def test_glossary_reaches_recast(monkeypatch):
    import methods
    mid = _material()
    cid = seed.make_card(seed.make_course(), "Q", "A", material_id=mid)
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: TERMS)
    generate.build_glossary(mid)
    seen = {}

    def fake(prompt, model):
        seen["prompt"] = prompt
        return {"front": "F2", "back": "B2"}
    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    generate.recast_card(cid, methods.get_method("qa"))
    assert "template=鋳型" in seen["prompt"]


def test_material_dict_exposes_glossary(client, monkeypatch):
    mid = _material()
    assert client.get(f"/api/materials/{mid}").get_json()["glossary"] == []
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: TERMS)
    generate.build_glossary(mid)
    body = client.get(f"/api/materials/{mid}").get_json()
    assert [t["ja"] for t in body["glossary"]] == ["ヌクレオチド", "鋳型"]


def test_glossary_does_not_touch_card_identity(monkeypatch):
    """D-5: a glossary feeds prompts only — it must never change a content_hash."""
    mid = _material()
    co = seed.make_course()
    cid = seed.make_card(co, "光合成とは？", "光で有機物を作る過程", material_id=mid)
    before = db.query_one("SELECT content_hash FROM cards WHERE id=?", (cid,))["content_hash"]
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: TERMS)
    generate.build_glossary(mid)
    after = db.query_one("SELECT content_hash FROM cards WHERE id=?", (cid,))["content_hash"]
    assert after == before


def test_ingest_glossary_terms_tolerates_bad_json():
    mid = _material()
    db.write("UPDATE materials SET glossary_json=? WHERE id=?", ("not json", mid))
    r = db.query_one("SELECT * FROM materials WHERE id=?", (mid,))
    assert ingest._glossary_terms(r) == []
