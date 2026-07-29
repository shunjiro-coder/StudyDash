"""Phase I: material summary (要点まとめ) stored as a study_guide attached to a
specific material (study_guides.material_id, added by an additive migration)."""

import pytest

import app as app_module
import db
import generate
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def test_study_guides_has_material_id():
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(study_guides)")}
    assert "material_id" in cols


def test_generate_summary(monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="細胞の教材本文")
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"summary_md": "## 要点\n- A\n- B"})
    g = generate.generate_summary(mid, scope="全体")
    assert g["material_id"] == mid
    assert g["content_md"].startswith("## 要点") and g["scope_desc"] == "全体"


def test_generate_summary_empty_raises(monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="本文")
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"summary_md": ""})
    with pytest.raises(ValueError):
        generate.generate_summary(mid)


def test_summary_endpoint_and_detail(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="本文")
    # no summary yet
    assert client.get(f"/api/materials/{mid}").get_json()["summary_guide"] is None
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"summary_md": "## まとめ\n- x"})
    r = client.post(f"/api/materials/{mid}/summary", json={"scope": "第2章"})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert r.get_json()["summary"]["content_md"].startswith("## まとめ")
    d = client.get(f"/api/materials/{mid}").get_json()
    assert d["summary_guide"]["content_md"].startswith("## まとめ")
    assert d["summary_guide"]["scope_desc"] == "第2章"


def test_summary_failure_returns_ok_false(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="本文")
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *a, **k: {"summary_md": ""})
    r = client.post(f"/api/materials/{mid}/summary", json={})
    assert r.status_code == 200 and r.get_json()["ok"] is False


def test_summary_unknown_material_404(client):
    assert client.post("/api/materials/99999/summary", json={}).status_code == 404


def test_material_summary_not_in_course_guides(client, monkeypatch):
    # A material summary carries course_id (for ON DELETE CASCADE cleanup) but
    # must NOT pollute the course-scoped 要点まとめ list — else it leaks onto
    # sibling materials and duplicates on its own page. It surfaces only via
    # summary_guide. Genuine course guides (material_id NULL) still appear.
    co = seed.make_course()
    a = seed.make_material(co, extracted_text="A本文")
    b = seed.make_material(co, extracted_text="B本文")
    seed.make_study_guide(co, content_md="# コース要点")          # material_id NULL
    monkeypatch.setattr(generate, "_generate_with_retry",
                        lambda *ar, **k: {"summary_md": "## Aのまとめ"})
    client.post(f"/api/materials/{a}/summary", json={})
    # sibling B: course guides must NOT include A's material summary
    db_b = client.get(f"/api/materials/{b}").get_json()
    assert all("Aのまとめ" not in g["content_md"] for g in db_b["guides"])
    assert any("コース要点" in g["content_md"] for g in db_b["guides"])
    # A's own page: summary shows via summary_guide, not in guides
    db_a = client.get(f"/api/materials/{a}").get_json()
    assert db_a["summary_guide"]["content_md"] == "## Aのまとめ"
    assert all("Aのまとめ" not in g["content_md"] for g in db_a["guides"])
