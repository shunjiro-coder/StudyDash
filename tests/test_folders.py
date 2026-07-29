"""Phase I: note folders — user-named containers to organize notes freely. A note
has at most one folder (docs.folder_id, NULL = 未分類). Deleting a folder un-files
its notes (they survive as 未分類), never deletes them."""

import pytest

import app as app_module
import db
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def test_folders_table_and_docs_column():
    fcols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(folders)")}
    dcols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(docs)")}
    assert {"name", "position"} <= fcols and "folder_id" in dcols


def test_folder_crud(client):
    r = client.post("/api/folders", json={"name": "数学"})
    assert r.status_code == 201
    fid = r.get_json()["id"]
    assert client.get("/api/folders").get_json()[0]["name"] == "数学"
    assert client.patch(f"/api/folders/{fid}", json={"name": "算数"}).status_code == 200
    assert client.get("/api/folders").get_json()[0]["name"] == "算数"
    # empty name rejected on create and rename
    assert client.post("/api/folders", json={"name": " "}).status_code == 400
    assert client.patch(f"/api/folders/{fid}", json={"name": ""}).status_code == 400


def test_assign_doc_to_folder(client):
    fid = client.post("/api/folders", json={"name": "英語"}).get_json()["id"]
    did = seed.make_doc(title="文法ノート")
    r = client.patch(f"/api/docs/{did}", json={"folder_id": fid})
    assert r.status_code == 200 and r.get_json()["folder_id"] == fid
    assert client.get("/api/folders").get_json()[0]["doc_count"] == 1
    # un-file (folder_id -> None)
    r2 = client.patch(f"/api/docs/{did}", json={"folder_id": None})
    assert r2.get_json()["folder_id"] is None
    assert client.get("/api/folders").get_json()[0]["doc_count"] == 0


def test_assign_unknown_folder_400(client):
    did = seed.make_doc()
    assert client.patch(f"/api/docs/{did}", json={"folder_id": 9999}).status_code == 400


def test_delete_folder_unfiles_notes(client):
    fid = client.post("/api/folders", json={"name": "理科"}).get_json()["id"]
    did = seed.make_doc(title="n")
    client.patch(f"/api/docs/{did}", json={"folder_id": fid})
    assert client.delete(f"/api/folders/{fid}").status_code == 200
    # note survives, un-filed; folder is gone
    doc = db.query_one("SELECT * FROM docs WHERE id=?", (did,))
    assert doc is not None and doc["folder_id"] is None
    assert client.get("/api/folders").get_json() == []


def test_folder_position_bad_value_400(client):
    # a non-numeric position must be a clean 400, not a 500
    fid = client.post("/api/folders", json={"name": "F"}).get_json()["id"]
    assert client.patch(f"/api/folders/{fid}", json={"position": "abc"}).status_code == 400


def test_doc_dict_exposes_folder_id(client):
    fid = client.post("/api/folders", json={"name": "F"}).get_json()["id"]
    did = seed.make_doc()
    db.write("UPDATE docs SET folder_id=? WHERE id=?", (fid, did))
    d = client.get(f"/api/docs/{did}").get_json()
    assert d["folder_id"] == fid
