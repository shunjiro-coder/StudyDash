"""Phase I: output-language setting for AI-generated study content (auto|ja|en).
Governs card/quiz/summary generation; 'auto' follows the material, ja/en force it.
SETTINGS_PATH is redirected to a temp file so the real settings.json is untouched."""

import pytest

import app as app_module
import db
import generate


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    app_module.app.testing = True
    return app_module.app.test_client()


def test_default_is_auto(client):
    assert client.get("/api/settings").get_json()["content_lang"] == "auto"
    assert db.content_lang() == "auto"


def test_set_and_persist(client):
    r = client.patch("/api/settings", json={"content_lang": "en"})
    assert r.status_code == 200 and r.get_json()["content_lang"] == "en"
    assert client.get("/api/settings").get_json()["content_lang"] == "en"
    assert db.content_lang() == "en"


def test_invalid_value_400(client):
    assert client.patch("/api/settings", json={"content_lang": "fr"}).status_code == 400
    assert client.patch("/api/settings", json={}).status_code == 400


def test_meta_exposes_content_lang(client):
    client.patch("/api/settings", json={"content_lang": "ja"})
    assert client.get("/api/meta").get_json()["content_lang"] == "ja"


def test_lang_line_forces_language():
    # explicit ja/en override the directive regardless of the saved setting
    assert "Japanese" in generate._lang_line("ja")
    assert "English" in generate._lang_line("en")
    assert "SAME language" in generate._lang_line("auto")


def test_lang_line_reaches_quiz_prompt(client, monkeypatch):
    from tests import seed
    client.patch("/api/settings", json={"content_lang": "en"})
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="本文")
    captured = {}

    def fake(prompt, model):
        captured["prompt"] = prompt
        return {"questions": [{"type": "written", "question": "Q", "answer": "A"}]}

    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    generate.generate_quiz(mid, "written")            # lang=None -> saved setting (en)
    assert "English" in captured["prompt"]
