"""Phase L: per-material study deadlines pace the daily new-card intake.

per_day = ceil(unseen / days-left-inclusive-of-today). Materials without a
deadline keep the fixed review_new_cap exactly as before.
"""

import pytest

import app as app_module
import db
import srs
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def test_target_date_column_exists():
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(materials)")}
    assert "target_date" in cols


# -------------------- days_left --------------------
def test_days_left_is_inclusive_of_today(clock):
    # frozen "now" is 2026-07-28 12:00 UTC -> local date is derived from it
    today = db.now_dt().astimezone().date().isoformat()
    assert srs.days_left(today) == 1                      # due today = finish today
    from datetime import timedelta
    tomorrow = (db.now_dt().astimezone().date() + timedelta(days=1)).isoformat()
    assert srs.days_left(tomorrow) == 2


def test_days_left_clamps_past_and_junk_to_one(clock):
    assert srs.days_left("2000-01-01") == 1               # cramming, not crashing
    assert srs.days_left("junk") == 1
    assert srs.days_left(None) == 1


# -------------------- paced_materials --------------------
def _deck(n, target=None):
    co = seed.make_course()
    mid = seed.make_material(co)
    if target:
        db.write("UPDATE materials SET target_date=? WHERE id=?", (target, mid))
    for i in range(n):
        seed.make_card(co, f"q{i}", f"a{i}", material_id=mid, state="new")
    return mid


def _in_days(d):
    from datetime import timedelta
    return (db.now_dt().astimezone().date() + timedelta(days=d)).isoformat()


def test_paced_materials_ceils(clock):
    mid = _deck(10, target=_in_days(2))                    # 10 cards / 3 days -> 4
    p = srs.paced_materials()
    assert [(x["material_id"], x["days_left"], x["per_day"]) for x in p] == [(mid, 3, 4)]


def test_paced_materials_ignores_untargeted_and_exhausted(clock):
    _deck(5)                                               # no target -> not paced
    co = seed.make_course()
    mid = seed.make_material(co)
    db.write("UPDATE materials SET target_date=? WHERE id=?", (_in_days(3), mid))
    seed.make_card(co, "done", "done", material_id=mid, state="review")   # no new left
    assert srs.paced_materials() == []


# -------------------- get_queue pacing --------------------
def test_queue_paces_targeted_material_beyond_the_global_cap(clock):
    mid = _deck(300, target=_in_days(9))                   # 300/10日 -> 30/day > cap 10
    q = srs.get_queue()
    from_target = [c for c in q["cards"] if c["material_id"] == mid]
    assert len(from_target) == 30
    assert q["new_count"] == 30
    assert q["new_cap"] == 10 and "due_cap" in q           # additive keys exposed
    assert q["paced"][0]["per_day"] == 30


def test_queue_untargeted_materials_keep_the_fixed_cap(clock):
    _deck(50)                                              # no deadline
    q = srs.get_queue()
    assert q["new_count"] == 10                            # review_new_cap default


def test_queue_mixes_paced_and_unpaced(clock):
    paced_mid = _deck(6, target=_in_days(1))               # 6/2日 -> 3/day
    _deck(50)                                              # unpaced -> global cap 10
    q = srs.get_queue()
    by_mat = {}
    for c in q["cards"]:
        by_mat[c["material_id"]] = by_mat.get(c["material_id"], 0) + 1
    assert by_mat[paced_mid] == 3
    assert q["new_count"] == 13                            # 3 paced + 10 capped


# -------------------- target endpoint --------------------
def test_target_endpoint_sets_normalizes_and_clears(client, clock):
    mid = _deck(4)
    body = client.post(f"/api/materials/{mid}/target",
                       json={"date": _in_days(3)}).get_json()
    assert body["ok"] and body["pacing"]["per_day"] == 1   # 4 cards / 4 days
    assert client.post(f"/api/materials/{mid}/target",
                       json={"date": "2026-9-5"}).get_json()["target_date"] == "2026-09-05"
    body = client.post(f"/api/materials/{mid}/target", json={"date": None}).get_json()
    assert body["ok"] and body["target_date"] is None
    assert srs.paced_materials() == []


def test_target_endpoint_rejects_junk(client):
    mid = _deck(1)
    for bad in ("2026-13-99", "not-a-date", 5):
        res = client.post(f"/api/materials/{mid}/target", json={"date": bad})
        assert res.status_code == 200 and res.get_json()["ok"] is False
    assert client.post("/api/materials/999999/target",
                       json={"date": "2026-09-01"}).status_code == 404


def test_review_materials_exposes_pacing(client, clock):
    mid = _deck(9, target=_in_days(2))                     # 9/3日 -> 3
    groups = client.get("/api/review/materials").get_json()
    g = [x for x in groups if x["material_id"] == mid][0]
    assert g["days_left"] == 3 and g["per_day"] == 3
    assert g["target_date"] == _in_days(2)


def test_pacing_never_touches_card_identity_or_srs(clock):
    mid = _deck(5, target=_in_days(2))
    before = [dict(r) for r in db.query(
        "SELECT content_hash, state, repetitions FROM cards WHERE material_id=? ORDER BY id", (mid,))]
    srs.get_queue()
    srs.paced_materials()
    after = [dict(r) for r in db.query(
        "SELECT content_hash, state, repetitions FROM cards WHERE material_id=? ORDER BY id", (mid,))]
    assert after == before
