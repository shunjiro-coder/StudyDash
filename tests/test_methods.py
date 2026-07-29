"""Phase G2: study-method catalog (built-ins + custom presets) and AI recast of
a card into a chosen method."""

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


def test_migration_creates_study_methods_table():
    cols = {r["name"] for r in
            db.get_conn().execute("PRAGMA table_info(study_methods)")}
    assert {"id", "name", "base", "instruction"} <= cols


def test_builtins_listed_and_resolvable(client):
    ids = {m["id"] for m in client.get("/api/methods").get_json()}
    assert {"qa", "term", "cloze", "steps", "elaborate"} <= ids
    assert methods.get_method("cloze")["card_type"] == "cloze"
    assert methods.get_method("nope") is None


def test_custom_method_crud(client):
    r = client.post("/api/methods", json={"name": "語源つき", "base": "term",
                                          "instruction": "語源も添える"})
    assert r.status_code == 201
    m = r.get_json()
    assert m["name"] == "語源つき" and m["id"].startswith("custom:")
    assert methods.get_method(m["id"])["instruction"] == "語源も添える"
    assert any(x["id"] == m["id"] for x in client.get("/api/methods").get_json())
    cid = m["id"].split(":")[1]
    assert client.delete(f"/api/methods/{cid}").status_code == 200
    assert methods.get_method(m["id"]) is None


def test_create_requires_name(client):
    assert client.post("/api/methods", json={"name": ""}).status_code == 400


def test_recast_creates_new_card_and_suspends_original(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co)
    cid = seed.make_card(co, "光合成とは？", "光で有機物を作る",
                         state="review", material_id=mid)
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"front": "光合成は___で有機物を作る", "back": "光"})
    r = client.post(f"/api/cards/{cid}/recast", json={"method": "cloze"})
    assert r.status_code == 200 and r.get_json()["ok"]
    new = r.get_json()["card"]
    # Recast always enters as a fresh 'new' card (NOT a copy of the original's
    # state) so it surfaces in the queue regardless of the original's SRS state.
    assert new["card_type"] == "cloze" and new["state"] == "new"
    assert new["front"] == "光合成は___で有機物を作る" and new["id"] != cid
    assert db.query_one("SELECT state FROM cards WHERE id=?", (cid,))["state"] == "suspended"


def test_recast_collision_does_not_suspend_original(client, monkeypatch):
    # If the recast content collides with an existing card (INSERT OR IGNORE
    # no-op) the original must NOT be suspended — else it's lost with no
    # replacement. The endpoint reports ok:false and leaves the source intact.
    co = seed.make_course()
    cid = seed.make_card(co, "元の問い", "元の答え", state="new")
    # A pre-existing card whose content the recast will collide with.
    seed.make_card(co, "既存の問い", "既存の答え", state="new")
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"front": "既存の問い", "back": "既存の答え"})
    r = client.post(f"/api/cards/{cid}/recast", json={"method": "cloze"})
    assert r.status_code == 200 and r.get_json()["ok"] is False
    assert db.query_one("SELECT state FROM cards WHERE id=?", (cid,))["state"] == "new"


def test_recast_unknown_method_400(client):
    co = seed.make_course()
    cid = seed.make_card(co, "Q", "A")
    assert client.post(f"/api/cards/{cid}/recast",
                       json={"method": "zzz"}).status_code == 400
