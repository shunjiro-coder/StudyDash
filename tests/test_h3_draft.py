"""Phase H3: smart 'make cards from this material' — auto-analyze with an optional
range + free-text instruction, drafting PROPOSED cards (G1 gate; nothing is studied
until the user confirms). Generation is one AI call (mocked here)."""

import pytest

import app as app_module
import db
import generate
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def test_draft_creates_proposed_cards(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="光合成の教材本文")
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: {
        "cards": [
            {"front": "光合成とは？", "back": "光で有機物を作る", "topic": "光合成", "card_type": "qa"},
            {"front": "細胞小器官を挙げよ", "back": "核\nミト", "topic": "細胞", "card_type": "list",
             "media_json": {"items": ["核", "ミトコンドリア"]}},
        ]})
    r = client.post(f"/api/materials/{mid}/draft", json={})
    assert r.status_code == 200 and r.get_json()["ok"]
    j = r.get_json()
    assert j["added"] == 2 and set(j["topics"]) == {"光合成", "細胞"}
    # drafts are PROPOSED (never auto-queued)
    states = [row["state"] for row in db.query("SELECT state FROM cards WHERE material_id=?", (mid,))]
    assert states and all(s == "proposed" for s in states)


def test_draft_passes_scope_and_instruction_to_prompt(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="本文")
    cap = {}

    def fake(prompt, model):
        cap["p"] = prompt
        return {"cards": [{"front": "Q", "back": "A", "card_type": "qa"}]}

    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    client.post(f"/api/materials/{mid}/draft", json={"scope": "第3章", "instruction": "計算多めで"})
    assert "第3章" in cap["p"] and "計算多めで" in cap["p"]


def test_draft_no_course_returns_ok_false(client):
    mid = seed.make_material(None, extracted_text="本文")     # material with no course
    r = client.post(f"/api/materials/{mid}/draft", json={})
    assert r.status_code == 200 and r.get_json()["ok"] is False


def test_draft_empty_text_returns_ok_false(client):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="")
    r = client.post(f"/api/materials/{mid}/draft", json={})
    assert r.status_code == 200 and r.get_json()["ok"] is False


def test_draft_unknown_material_404(client):
    assert client.post("/api/materials/99999/draft", json={}).status_code == 404
