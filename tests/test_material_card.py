"""Phase E4 foundation: cards.source_loc column + POST /api/materials/<id>/card.

Additive over the existing extract/generate pipeline — a card sourced from a
material (optionally with a precise location) flows into the same review queue.
"""

import pytest

import app as app_module
import db


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def _material(status="done", course_id=None):
    return db.write(
        "INSERT INTO materials (kind, original_path, status, course_id, created_at) "
        "VALUES ('photo','uploads/x.jpg',?,?,?)",
        (status, course_id, db.now_utc_iso()))


def test_source_loc_column_added_by_migration():
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(cards)")}
    assert "source_loc" in cols


def test_add_card_with_location(client):
    mid = _material()
    r = client.post(f"/api/materials/{mid}/card", json={
        "front": "光合成とは？", "back": "光で有機物を作る反応",
        "source_quote": "光合成は光を使い…",
        "source_loc": {"quote": "光合成は光を使い…", "char_start": 3, "char_end": 9},
    })
    assert r.status_code == 201
    card = r.get_json()
    assert card["front"] == "光合成とは？"
    assert card["state"] == "new"                      # enters the queue
    assert card["source_loc"] == {"quote": "光合成は光を使い…", "char_start": 3, "char_end": 9}
    # visible under the material and linked to it
    detail = client.get(f"/api/materials/{mid}").get_json()
    assert any(c["front"] == "光合成とは？" for c in detail["cards"])
    row = db.query_one("SELECT material_id FROM cards WHERE id=?", (card["id"],))
    assert row["material_id"] == mid


def test_add_card_dedups_within_course(client):
    cid = db.write("INSERT INTO courses (name, created_at) VALUES ('生物', ?)",
                   (db.now_utc_iso(),))
    mid = _material(course_id=cid)
    payload = {"front": "Q", "back": "A"}
    a = client.post(f"/api/materials/{mid}/card", json=payload)
    b = client.post(f"/api/materials/{mid}/card", json=payload)
    assert a.status_code == 201 and b.status_code == 201
    n = db.query_one("SELECT COUNT(*) n FROM cards WHERE material_id=?", (mid,))["n"]
    assert n == 1                                      # content_hash dedup (D-5)


def test_add_card_validates(client):
    mid = _material()
    assert client.post(f"/api/materials/{mid}/card",
                       json={"front": "", "back": "A"}).status_code == 400
    assert client.post("/api/materials/999999/card",
                       json={"front": "Q", "back": "A"}).status_code == 404
