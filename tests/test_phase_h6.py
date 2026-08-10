"""H6: image occlusion. Rectangle drawing only — no AI.

The load-bearing constraint: every card made from one image shares the same
question text, so if the region identity lived only in media_json (which is
EXCLUDED from the D-5 hash) they would all hash identically and INSERT OR IGNORE
would collapse them into one card. Changing the hash formula is the forbidden
deviation, so the region's LABEL is the card's `back`.
"""

import json

import pytest

import app as app_module
import db
import generate
from tests import seed

R1 = {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "label": "海馬"}
R2 = {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2, "label": "扁桃体"}


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def _image_material():
    return seed.make_material(seed.make_course(), kind="photo",
                              original_path="uploads/brain.jpg")


# -------------------- the D-5 constraint --------------------
def test_one_card_per_region_each_with_a_distinct_hash():
    mid = _image_material()
    res = generate.create_occlusion_cards(mid, [R1, R2])
    assert len(res["created"]) == 2 and res["duplicates"] == 0
    hashes = {c["content_hash"] for c in res["created"]}
    assert len(hashes) == 2                      # NOT collapsed by INSERT OR IGNORE
    assert {c["back"] for c in res["created"]} == {"海馬", "扁桃体"}


def test_hash_still_follows_the_unchanged_d5_formula():
    mid = _image_material()
    res = generate.create_occlusion_cards(mid, [R1])
    c = res["created"][0]
    assert c["content_hash"] == db.content_hash(generate.OCCLUSION_FRONT, "海馬")


def test_rect_geometry_is_excluded_from_identity():
    """media_json holds the rectangles; moving a box must not re-identify a card."""
    mid = _image_material()
    a = generate.create_occlusion_cards(mid, [R1])["created"][0]
    db.write("DELETE FROM cards WHERE id=?", (a["id"],))
    moved = dict(R1, x=0.9, y=0.9, w=0.05, h=0.05)
    b = generate.create_occlusion_cards(mid, [moved])["created"][0]
    assert a["content_hash"] == b["content_hash"]        # same question, same answer


def test_same_label_twice_is_reported_as_duplicate_not_lost_silently():
    mid = _image_material()
    res = generate.create_occlusion_cards(mid, [R1, dict(R2, label="海馬")])
    assert len(res["created"]) == 1 and res["duplicates"] == 1


def test_each_card_points_at_its_own_region():
    mid = _image_material()
    res = generate.create_occlusion_cards(mid, [R1, R2])
    targets = sorted(json.loads(c["media_json"])["occlusion"]["target"]
                     for c in res["created"])
    assert targets == [0, 1]
    # every card still carries ALL rectangles, so the others render as masks
    for c in res["created"]:
        assert len(json.loads(c["media_json"])["occlusion"]["rects"]) == 2


# -------------------- payload validation --------------------
def test_coordinates_are_clamped_and_bad_rects_dropped():
    occ = generate._clean_occlusion({"image": "uploads/x.jpg", "rects": [
        {"x": -5, "y": 0.5, "w": 9, "h": 0.2, "label": "はみ出し"},
        {"x": 0.1, "y": 0.1, "w": 0, "h": 0.2, "label": "面積ゼロ"},
        {"x": "junk", "y": 0.1, "w": 0.2, "h": 0.2, "label": "文字"},
        {"x": 0.1, "y": 0.1, "w": float("nan"), "h": 0.2, "label": "NaN"},
    ]})
    assert len(occ["rects"]) == 1
    r = occ["rects"][0]
    assert r["x"] == 0.0 and r["y"] == 0.5
    assert 0 < r["w"] <= 1.0 and r["x"] + r["w"] <= 1.0     # kept inside the image


def test_occlusion_needs_an_image_and_at_least_one_rect():
    assert generate._clean_occlusion({"image": "", "rects": [R1]}) is None
    assert generate._clean_occlusion({"image": "uploads/x.jpg", "rects": []}) is None
    assert generate._clean_occlusion("not a dict") is None


def test_out_of_range_target_falls_back_to_the_first_region():
    occ = generate._clean_occlusion({"image": "uploads/x.jpg", "rects": [R1], "target": 99})
    assert occ["target"] == 0


def test_rects_are_capped():
    many = [dict(R1, y=i / 100.0, label=f"L{i}") for i in range(50)]
    occ = generate._clean_occlusion({"image": "uploads/x.jpg", "rects": many})
    assert len(occ["rects"]) == generate.OCCLUSION_MAX_RECTS


def test_every_region_needs_a_label():
    mid = _image_material()
    with pytest.raises(ValueError):
        generate.create_occlusion_cards(mid, [dict(R1, label="")])


def test_pdf_and_imageless_materials_are_refused():
    co = seed.make_course()
    pdf = seed.make_material(co, kind="pdf", original_path="uploads/a.pdf")
    with pytest.raises(ValueError):
        generate.create_occlusion_cards(pdf, [R1])
    noimg = seed.make_material(co, kind="photo", original_path="")
    with pytest.raises(ValueError):
        generate.create_occlusion_cards(noimg, [R1])


def test_missing_material_is_none():
    assert generate.create_occlusion_cards(999999, [R1]) is None


# -------------------- endpoint --------------------
def test_occlusion_endpoint(client):
    mid = _image_material()
    body = client.post(f"/api/materials/{mid}/occlusion",
                       json={"rects": [R1, R2]}).get_json()
    assert body["ok"] and len(body["created"]) == 2
    assert body["created"][0]["card_type"] == "occlusion"


def test_occlusion_endpoint_rejects_empty_and_missing(client):
    mid = _image_material()
    assert client.post(f"/api/materials/{mid}/occlusion", json={"rects": []}).get_json()["ok"] is False
    res = client.post(f"/api/materials/{mid}/occlusion", json={"rects": [dict(R1, label="")]})
    assert res.status_code == 200 and res.get_json()["ok"] is False   # not a 500
    assert client.post("/api/materials/999999/occlusion", json={"rects": [R1]}).status_code == 404


def test_occlusion_cards_enter_the_queue_as_new(client):
    mid = _image_material()
    client.post(f"/api/materials/{mid}/occlusion", json={"rects": [R1]})
    row = db.query_one("SELECT state, card_type FROM cards WHERE card_type='occlusion'")
    assert row["state"] == "new"
