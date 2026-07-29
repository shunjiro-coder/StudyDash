"""Phase I: material comprehension quizzes (written / mixed) — a one-shot test
to grasp a material's whole range, deliberately SEPARATE from the SRS queue.
Generation is a single AI call (mocked here via generate._generate_with_retry)."""

import json

import pytest

import app as app_module
import db
import generate
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def test_quizzes_table_exists():
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(quizzes)")}
    assert {"material_id", "format", "questions_json"} <= cols


def test_auto_count_scales_by_length():
    assert generate._auto_quiz_count("") == 10                 # floor QUIZ_MIN
    assert generate._auto_quiz_count("x" * 3000) == 20         # ~14 -> round up to 20
    assert generate._auto_quiz_count("x" * 100000) == 50       # capped at QUIZ_MAX


def test_clean_written_demotes_choice():
    qs = generate._clean_quiz_questions(
        [{"type": "choice", "question": "Q", "choices": ["a", "b"], "answer": "a"}],
        "written")
    assert qs == [{"type": "written", "question": "Q", "answer": "a"}]


def test_clean_mixed_keeps_valid_choice_demotes_bad():
    qs = generate._clean_quiz_questions([
        {"type": "choice", "question": "Q1", "choices": ["a", "b", "c", "d"], "answer": "b"},
        {"type": "choice", "question": "Q2", "choices": ["a", "b"], "answer": "z"},  # answer not in choices
        {"type": "written", "question": "Q3", "answer": "A3"},
        {"question": "", "answer": "x"},        # dropped (no question)
    ], "mixed")
    assert [q["type"] for q in qs] == ["choice", "written", "written"]
    assert qs[0]["choices"] == ["a", "b", "c", "d"]


def test_generate_quiz_written_demotes_choices(monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="光合成の教材本文" * 20)
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: {
        "questions": [{"type": "written", "question": "Q1", "answer": "A1"},
                      {"type": "choice", "question": "Q2",
                       "choices": ["a", "b", "c", "d"], "answer": "a"}]})
    qz = generate.generate_quiz(mid, "written")
    qs = json.loads(qz["questions_json"])
    assert qz["format"] == "written"
    assert all(q["type"] == "written" for q in qs)   # choice demoted in written mode


def test_generate_quiz_empty_text_raises():
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="")
    with pytest.raises(ValueError):
        generate.generate_quiz(mid, "written")


def test_quiz_endpoint_and_material_detail(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="本文" * 50)
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: {
        "questions": [{"type": "written", "question": "説明せよ", "answer": "模範"}]})
    r = client.post(f"/api/materials/{mid}/quiz", json={"format": "written", "scope": "第1章"})
    assert r.status_code == 200 and r.get_json()["ok"]
    quiz = r.get_json()["quiz"]
    assert quiz["scope_desc"] == "第1章" and len(quiz["questions"]) == 1
    # material detail + GET both surface the latest quiz
    assert client.get(f"/api/materials/{mid}").get_json()["quiz"]["id"] == quiz["id"]
    assert client.get(f"/api/materials/{mid}/quiz").get_json()["quiz"]["id"] == quiz["id"]


def test_quiz_count_override_reaches_prompt(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="本文")
    captured = {}

    def fake(prompt, model):
        captured["prompt"] = prompt
        return {"questions": [{"type": "written", "question": f"Q{i}", "answer": "A"}
                              for i in range(30)]}

    monkeypatch.setattr(generate, "_generate_with_retry", fake)
    r = client.post(f"/api/materials/{mid}/quiz", json={"format": "written", "count": 30})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert "30" in captured["prompt"]        # the requested count reached the prompt


def test_quiz_generation_failure_returns_ok_false(client, monkeypatch):
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="本文")

    def boom(*a, **k):
        raise ValueError("parse fail")

    monkeypatch.setattr(generate, "_generate_with_retry", boom)
    r = client.post(f"/api/materials/{mid}/quiz", json={"format": "written"})
    assert r.status_code == 200 and r.get_json()["ok"] is False


def test_quiz_unknown_material_404(client):
    assert client.post("/api/materials/99999/quiz", json={}).status_code == 404
