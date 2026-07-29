"""Phase G1: proposal / confirmation gate. Uploads draft cards as state
='proposed' (never auto-queued); the user approves which items to actually
study. Approved -> 'new' (queue); the rest -> 'suspended' (recoverable).
Proposed cards must stay invisible to every study/count query until approved.
"""

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


def test_generated_cards_are_proposed_not_queued(monkeypatch):
    co = seed.make_course(subject_type="memo")
    mid = seed.make_material(co, extracted_text="光合成は光で有機物を合成する。")
    obj = {"study_guide_md": "", "cards": [
        {"front": "光合成とは？", "back": "光で有機物を作る", "source_quote": "有機物を合成"}]}
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: obj)
    generate.generate_for_material(mid)

    card = db.query_one("SELECT * FROM cards WHERE material_id=?", (mid,))
    assert card["state"] == "proposed"                       # drafted, not queued
    q = srs.get_queue()
    assert all(c["id"] != card["id"] for c in q["cards"])     # not in the queue
    assert srs.mastery(co)["total"] == 0                      # not counted as real


def test_approve_selected_promotes_and_suspends_rest(client):
    co = seed.make_course()
    mid = seed.make_material(co)
    a = seed.make_card(co, "A", "a", state="proposed", material_id=mid)
    b = seed.make_card(co, "B", "b", state="proposed", material_id=mid)
    c = seed.make_card(co, "C", "c", state="proposed", material_id=mid)

    r = client.post(f"/api/materials/{mid}/approve", json={"card_ids": [a, b]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["approved"] == 2 and body["dropped"] == 1
    assert db.query_one("SELECT state FROM cards WHERE id=?", (a,))["state"] == "new"
    assert db.query_one("SELECT state FROM cards WHERE id=?", (b,))["state"] == "new"
    assert db.query_one("SELECT state FROM cards WHERE id=?", (c,))["state"] == "suspended"


def test_approve_all_when_card_ids_omitted(client):
    co = seed.make_course()
    mid = seed.make_material(co)
    a = seed.make_card(co, "A", "a", state="proposed", material_id=mid)
    b = seed.make_card(co, "B", "b", state="proposed", material_id=mid)
    r = client.post(f"/api/materials/{mid}/approve", json={})
    assert r.get_json()["approved"] == 2
    for cid in (a, b):
        assert db.query_one("SELECT state FROM cards WHERE id=?", (cid,))["state"] == "new"


def test_material_dict_reports_proposed_count(client):
    co = seed.make_course()
    mid = seed.make_material(co)
    seed.make_card(co, "A", "a", state="proposed", material_id=mid)
    seed.make_card(co, "B", "b", state="new", material_id=mid)
    d = client.get(f"/api/materials/{mid}").get_json()
    assert d["proposed_count"] == 1


def test_approve_missing_material_404(client):
    assert client.post("/api/materials/999999/approve", json={}).status_code == 404
