"""db.text_cell is the single answer to a bug class that has now bitten four
places: JSON from a model can hold a number, a null, a bool or an object where
text belongs. str(None) is the string "None" — which reached a learner as a
selectable answer — and a bare .strip() on a non-string raises AttributeError
deep inside a worker, where it strands the material being processed."""

import pytest

import db
import generate
import ingest
from tests import seed


@pytest.mark.parametrize("value,expected", [
    ("  hello  ", "hello"),
    ("", ""),
    (5, "5"),            # a maths option / a numeric vocabulary entry is content
    (3.5, "3.5"),
    (None, ""),          # the "None" bug
    (True, ""),          # would have become "True"
    (False, ""),
    ({"a": 1}, ""),
    (["x"], ""),
])
def test_text_cell(value, expected):
    assert db.text_cell(value) == expected


def test_one_shared_definition():
    """generate._cell must not drift from db.text_cell."""
    assert generate._cell is db.text_cell


def test_quiz_choices_never_offer_the_string_none():
    raw = [{"type": "choice", "question": "Q", "answer": "A",
            "choices": ["A", None, "B", True]}]
    out = generate._clean_quiz_questions(raw, "mixed")
    assert out[0]["choices"] == ["A", "B"]


def test_quiz_survives_non_string_question_and_answer():
    raw = [{"type": "written", "question": None, "answer": "A"},
           {"type": "written", "question": "Q", "answer": {"x": 1}},
           {"type": "written", "question": 2024, "answer": 42}]
    out = generate._clean_quiz_questions(raw, "written")
    assert [(q["question"], q["answer"]) for q in out] == [("2024", "42")]


def test_extracted_cards_survive_a_numeric_entry():
    """A vocabulary list really can contain a numeric entry; it used to raise
    AttributeError out of the worker and strand the material."""
    co = seed.make_course()
    mid = seed.make_material(co)
    n = ingest._insert_extracted_cards(co, mid, [
        {"front": 2024, "back": "the year"},
        {"front": "word", "back": "definition"},
        {"front": None, "back": "dropped"},
        {"front": "no back", "back": None},
    ])
    assert n == 2
    fronts = {r["front"] for r in db.query("SELECT front FROM cards WHERE material_id=?", (mid,))}
    assert fronts == {"2024", "word"}


def test_bad_card_payload_fails_the_material_instead_of_stranding_it(monkeypatch):
    """worker._run swallows exceptions and reconcile() re-enqueues 'generating'
    rows at boot, so an unguarded raise here re-crashes forever."""
    co = seed.make_course()
    mid = seed.make_material(co, status="extracting")
    monkeypatch.setattr(ingest, "_extract_with_retry",
                        lambda *a, **k: ({"text": "t", "course_hint": "数学",
                                          "extracted_cards": [], "subject_type": "stem"}, ""))

    def boom(*a, **k):
        raise RuntimeError("malformed payload")
    monkeypatch.setattr(ingest, "_insert_extracted_cards", boom)
    ingest.ingest_handler({"material_id": mid})
    row = db.query_one("SELECT status, error_message FROM materials WHERE id=?", (mid,))
    assert row["status"] == "failed"          # NOT left in 'generating'
    assert "カード保存に失敗" in row["error_message"]
