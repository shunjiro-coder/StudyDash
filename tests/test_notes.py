"""Phase B1 tests: migration ledger, docs/rems CRUD, fractional ordering +
renormalize, daily get-or-create race-safety, reorder/indent/outdent + cycle
guard, cascade deletes, zoom + breadcrumb."""

import pytest

import app as app_module
import db
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def _rem_row(rid):
    return db.query_one("SELECT * FROM rems WHERE id=?", (rid,))


# --------------------------------------------------------------------------
# Migration ledger — additive + idempotent + resumable
# --------------------------------------------------------------------------
def test_migration_created_docs_and_rems():
    tables = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"docs", "rems", "schema_migrations"} <= tables


def test_double_init_is_idempotent():
    # fresh_db already ran init(); a second init must not error or duplicate.
    db.init()
    db.init()
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(rems)")}
    assert {"position", "parent_id", "text"} <= cols
    rows = db.query(
        "SELECT COUNT(*) n FROM schema_migrations WHERE version='b1_docs_rems'")
    assert rows[0]["n"] == 1


def test_migration_recorded_with_phase():
    r = db.query_one(
        "SELECT phase FROM schema_migrations WHERE version='b1_docs_rems'")
    assert r["phase"] == "B"


# --------------------------------------------------------------------------
# Docs CRUD
# --------------------------------------------------------------------------
def test_create_and_get_doc(client):
    r = client.post("/api/docs", json={"title": "数学ノート"})
    assert r.status_code == 201
    did = r.get_json()["id"]
    got = client.get(f"/api/docs/{did}").get_json()
    assert got["title"] == "数学ノート"
    assert got["rems"] == []
    assert got["is_daily"] is False


def test_list_docs_excludes_archived_by_default(client):
    a = seed.make_doc(title="生きてる")
    seed.make_doc(title="消えた", archived=1)
    ids = [d["id"] for d in client.get("/api/docs").get_json()]
    assert a in ids
    all_ids = [d["id"] for d in client.get("/api/docs?archived=1").get_json()]
    assert len(all_ids) == 2


def test_patch_doc_title_and_archive(client):
    did = seed.make_doc(title="旧")
    r = client.patch(f"/api/docs/{did}", json={"title": "新", "archived": True})
    body = r.get_json()
    assert body["title"] == "新" and body["archived"] is True


def test_patch_missing_doc_404(client):
    assert client.patch("/api/docs/99999", json={"title": "x"}).status_code == 404


def test_delete_doc_cascades_rems(client):
    did = seed.make_doc()
    r1 = seed.make_rem(did, "a", position=1.0)
    seed.make_rem(did, "b", parent_id=r1, position=1.0)
    assert client.delete(f"/api/docs/{did}").status_code == 200
    assert db.query_one("SELECT COUNT(*) n FROM rems WHERE doc_id=?", (did,))["n"] == 0


# --------------------------------------------------------------------------
# Daily — get-or-create, race-safe, local-day label
# --------------------------------------------------------------------------
def test_daily_get_or_create_is_stable(client, clock):
    d1 = client.get("/api/docs/daily").get_json()
    d2 = client.get("/api/docs/daily").get_json()
    assert d1["id"] == d2["id"]            # same day -> same doc, no duplicate
    assert d1["is_daily"] is True
    assert db.query_one("SELECT COUNT(*) n FROM docs WHERE is_daily=1")["n"] == 1


def test_daily_uses_local_calendar_day(client, clock):
    d = client.get("/api/docs/daily").get_json()
    local_today = db.now_dt().astimezone().strftime("%Y-%m-%d")
    assert d["daily_date"] == local_today


# --------------------------------------------------------------------------
# Rems — create, nest, order
# --------------------------------------------------------------------------
def test_create_rems_are_ordered_by_position(client):
    did = seed.make_doc()
    a = client.post("/api/rems", json={"doc_id": did}).get_json()["rem"]
    b = client.post("/api/rems", json={"doc_id": did, "after_id": a["id"]}).get_json()["rem"]
    c = client.post("/api/rems", json={"doc_id": did, "after_id": a["id"]}).get_json()["rem"]
    # c was inserted between a and b -> order a, c, b
    order = [r["id"] for r in client.get(f"/api/docs/{did}").get_json()["rems"]]
    assert order == [a["id"], c["id"], b["id"]]


def test_create_rem_at_start(client):
    did = seed.make_doc()
    a = client.post("/api/rems", json={"doc_id": did}).get_json()["rem"]
    b = client.post("/api/rems", json={"doc_id": did, "after_id": None}).get_json()["rem"]
    order = [r["id"] for r in client.get(f"/api/docs/{did}").get_json()["rems"]]
    assert order == [b["id"], a["id"]]


def test_create_child_rem(client):
    did = seed.make_doc()
    a = client.post("/api/rems", json={"doc_id": did}).get_json()["rem"]
    child = client.post("/api/rems", json={"doc_id": did, "parent_id": a["id"]}).get_json()["rem"]
    assert child["parent_id"] == a["id"]


def test_create_rem_bad_doc_400(client):
    assert client.post("/api/rems", json={"doc_id": 99999}).status_code == 400


def test_create_rem_parent_from_other_doc_400(client):
    d1, d2 = seed.make_doc(), seed.make_doc()
    r = seed.make_rem(d1)
    resp = client.post("/api/rems", json={"doc_id": d2, "parent_id": r})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Renormalize when float precision is exhausted; positions re-sync to client
# --------------------------------------------------------------------------
def test_insert_renormalizes_on_precision_exhaustion(client):
    did = seed.make_doc()
    # two siblings sharing an identical position: any midpoint == the endpoints.
    a = seed.make_rem(did, "a", position=1.0)
    b = seed.make_rem(did, "b", position=1.0)
    resp = client.post("/api/rems", json={"doc_id": did, "after_id": a}).get_json()
    assert resp["renormalized"] is not None
    ids = {x["id"] for x in resp["renormalized"]}
    assert ids == {a, b}                              # both siblings renumbered
    positions = [r["position"] for r in
                 client.get(f"/api/docs/{did}").get_json()["rems"]]
    assert positions == sorted(positions)
    assert len(set(positions)) == 3                   # all distinct now


# --------------------------------------------------------------------------
# Autosave batch — text only, atomic
# --------------------------------------------------------------------------
def test_batch_autosave_persists_text(client):
    did = seed.make_doc()
    a = seed.make_rem(did, "old", position=1.0)
    b = seed.make_rem(did, "old", position=2.0)
    r = client.post("/api/rems/batch", json={
        "doc_id": did,
        "items": [{"id": a, "text": "新A"}, {"id": b, "text": "新B"}]})
    assert r.get_json()["saved"] == 2
    assert _rem_row(a)["text"] == "新A"
    assert _rem_row(b)["text"] == "新B"


def test_batch_touches_doc_updated_at(client, clock):
    did = seed.make_doc()
    a = seed.make_rem(did, position=1.0)
    before = db.query_one("SELECT updated_at FROM docs WHERE id=?", (did,))["updated_at"]
    clock.advance(minutes=5)
    client.post("/api/rems/batch", json={"doc_id": did,
                                         "items": [{"id": a, "text": "x"}]})
    after = db.query_one("SELECT updated_at FROM docs WHERE id=?", (did,))["updated_at"]
    assert after > before


# --------------------------------------------------------------------------
# PATCH rem
# --------------------------------------------------------------------------
def test_patch_rem_fields(client):
    did = seed.make_doc()
    rid = seed.make_rem(did, "t", position=1.0)
    r = client.patch(f"/api/rems/{rid}", json={
        "text": "更新", "rem_type": "heading", "collapsed": True, "done": True})
    body = r.get_json()
    assert body["text"] == "更新" and body["rem_type"] == "heading"
    assert body["collapsed"] is True and body["done"] is True


def test_patch_rem_props_roundtrip(client):
    did = seed.make_doc()
    rid = seed.make_rem(did, position=1.0)
    client.patch(f"/api/rems/{rid}", json={"props": {"k": "v", "n": 3}})
    assert client.get(f"/api/rems/{rid}").get_json()["root"]["props"] == {"k": "v", "n": 3}


# --------------------------------------------------------------------------
# Reorder — indent/outdent/DnD + cycle guard
# --------------------------------------------------------------------------
def test_reorder_reparents(client):
    did = seed.make_doc()
    a = seed.make_rem(did, "a", position=1.0)
    b = seed.make_rem(did, "b", position=2.0)
    r = client.post("/api/rems/reorder", json={
        "rem_id": b, "new_parent_id": a, "after_id": None})
    assert r.get_json()["rem"]["parent_id"] == a


def test_reorder_into_own_subtree_rejected(client):
    did = seed.make_doc()
    a = seed.make_rem(did, "a", position=1.0)
    child = seed.make_rem(did, "child", parent_id=a, position=1.0)
    # moving a under its own child would create a cycle
    r = client.post("/api/rems/reorder", json={
        "rem_id": a, "new_parent_id": child, "after_id": None})
    assert r.status_code == 400


def test_reorder_missing_rem_404(client):
    r = client.post("/api/rems/reorder", json={"rem_id": 99999})
    assert r.status_code == 404


def test_reorder_parent_from_other_doc_400(client):
    d1, d2 = seed.make_doc(), seed.make_doc()
    r1 = seed.make_rem(d1, position=1.0)
    other = seed.make_rem(d2, position=1.0)
    resp = client.post("/api/rems/reorder", json={
        "rem_id": r1, "new_parent_id": other})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Delete rem cascades subtree
# --------------------------------------------------------------------------
def test_delete_rem_cascades_subtree(client):
    did = seed.make_doc()
    a = seed.make_rem(did, "a", position=1.0)
    b = seed.make_rem(did, "b", parent_id=a, position=1.0)
    seed.make_rem(did, "c", parent_id=b, position=1.0)
    assert client.delete(f"/api/rems/{a}").status_code == 200
    assert db.query_one("SELECT COUNT(*) n FROM rems WHERE doc_id=?", (did,))["n"] == 0


def test_delete_missing_rem_404(client):
    assert client.delete("/api/rems/99999").status_code == 404


# --------------------------------------------------------------------------
# Zoom — subtree + breadcrumb
# --------------------------------------------------------------------------
def test_zoom_terminates_on_cyclic_data(client):
    # A cycle is only reachable via a reorder race, but zoom must never hang on
    # one. Seed P.parent=Q and Q.parent=P directly, then confirm zoom returns
    # (the _subtree_ids seen-set breaks the loop) instead of spinning forever.
    did = seed.make_doc()
    p = seed.make_rem(did, "P", position=1.0)
    q = seed.make_rem(did, "Q", position=2.0)
    db.write("UPDATE rems SET parent_id=? WHERE id=?", (q, p))
    db.write("UPDATE rems SET parent_id=? WHERE id=?", (p, q))
    r = client.get(f"/api/rems/{p}")
    assert r.status_code == 200
    assert {x["id"] for x in r.get_json()["rems"]} == {p, q}   # each visited once


def test_reorder_opposite_move_after_first_is_rejected(client):
    # The atomic (locked) cycle re-check means the loser of two opposite moves
    # sees the winner's committed reparent; sequentially, the second is rejected.
    did = seed.make_doc()
    p = seed.make_rem(did, "P", position=1.0)
    q = seed.make_rem(did, "Q", position=2.0)
    assert client.post("/api/rems/reorder",
                       json={"rem_id": p, "new_parent_id": q}).status_code == 200
    assert client.post("/api/rems/reorder",
                       json={"rem_id": q, "new_parent_id": p}).status_code == 400


def test_zoom_returns_subtree_and_breadcrumb(client):
    did = seed.make_doc()
    a = seed.make_rem(did, "root", position=1.0)
    b = seed.make_rem(did, "mid", parent_id=a, position=1.0)
    c = seed.make_rem(did, "leaf", parent_id=b, position=1.0)
    sibling = seed.make_rem(did, "elsewhere", position=2.0)
    z = client.get(f"/api/rems/{b}").get_json()
    got_ids = {r["id"] for r in z["rems"]}
    assert got_ids == {b, c}                       # subtree only, not `sibling`
    assert sibling not in got_ids
    crumb_ids = [x["id"] for x in z["breadcrumb"]]
    assert crumb_ids == [a]                         # ancestor chain above b
    assert z["root"]["id"] == b
