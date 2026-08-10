"""A large PDF (a 327-word TOEFL vocabulary list) exposed two bugs: a flat 180s
AI timeout killed the transcription, and the material was then marked 'done'
despite having no text — so it looked finished, had zero cards, and offered no
retry."""

import os

import pytest

import ai
import app as app_module
import db
import generate
from tests import seed


@pytest.fixture
def client_():
    app_module.app.testing = True
    return app_module.app.test_client()


def test_no_text_stays_failed_so_it_can_be_retried():
    """The bug: this path used to write status='done', overwriting the 'failed'
    that extraction had correctly set."""
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="", status="failed")
    db.write("UPDATE materials SET error_message=? WHERE id=?",
             ("AI呼び出し失敗: claude timed out after 180s", mid))
    added, why = generate.generate_for_material(mid)
    row = db.query_one("SELECT status, error_message FROM materials WHERE id=?", (mid,))
    assert added == 0 and why == "no text"
    assert row["status"] == "failed"                    # NOT 'done'
    assert "timed out" in row["error_message"]          # the real cause is kept


def test_no_text_without_a_prior_error_gets_an_actionable_message():
    co = seed.make_course()
    mid = seed.make_material(co, extracted_text="")
    generate.generate_for_material(mid)
    row = db.query_one("SELECT status, error_message FROM materials WHERE id=?", (mid,))
    assert row["status"] == "failed" and row["error_message"]


def test_material_with_text_but_no_course_is_still_done():
    """That case really is nothing-to-do, and must not regress into 'failed'."""
    mid = seed.make_material(None, extracted_text="本文あり")
    added, why = generate.generate_for_material(mid)
    assert why == "no course"
    assert db.query_one("SELECT status FROM materials WHERE id=?", (mid,))["status"] == "done"


def test_timeout_scales_with_file_size(tmp_path):
    small = tmp_path / "small.pdf"
    small.write_bytes(b"x" * 1024)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    assert ai.timeout_for(str(small)) == ai.DEFAULT_TIMEOUT
    assert ai.timeout_for(str(big)) > ai.DEFAULT_TIMEOUT
    assert ai.timeout_for(str(big)) <= ai.MAX_TIMEOUT


def test_timeout_never_exceeds_the_cap(tmp_path):
    huge = tmp_path / "huge.pdf"
    huge.write_bytes(b"x" * 1024)
    os.truncate(str(huge), 500 * 1024 * 1024)          # sparse 500MB
    assert ai.timeout_for(str(huge)) == ai.MAX_TIMEOUT


def test_timeout_defaults_without_a_path_and_survives_a_missing_file():
    assert ai.timeout_for() == ai.DEFAULT_TIMEOUT
    assert ai.timeout_for("/nope/does-not-exist.pdf") == ai.DEFAULT_TIMEOUT


@pytest.mark.parametrize("val,expected", [(600, 600), ("300", 300), (5, 30), (99999, ai.MAX_TIMEOUT)])
def test_timeout_setting_overrides_and_is_clamped(monkeypatch, val, expected):
    monkeypatch.setattr(db, "load_settings", lambda: {"ai_timeout_sec": val})
    assert ai.timeout_for("/whatever") == expected


def test_bad_timeout_setting_falls_back(monkeypatch):
    monkeypatch.setattr(db, "load_settings", lambda: {"ai_timeout_sec": "junk"})
    assert ai.timeout_for() == ai.DEFAULT_TIMEOUT


# -------------------- deleting materials from the list --------------------
def test_delete_material_keeps_its_cards_by_default(client_):
    co = seed.make_course()
    mid = seed.make_material(co)
    cid = seed.make_card(co, "Q", "A", material_id=mid, state="review", repetitions=3)
    body = client_.delete(f"/api/materials/{mid}").get_json()
    assert body["ok"] and body["deleted_cards"] == 0
    card = db.query_one("SELECT material_id, state, repetitions FROM cards WHERE id=?", (cid,))
    assert card is not None                       # the card survives
    assert card["material_id"] is None            # detached, not destroyed
    assert card["repetitions"] == 3               # review history intact


def test_delete_material_with_cards_flag_removes_them(client_):
    co = seed.make_course()
    mid = seed.make_material(co)
    seed.make_card(co, "Q", "A", material_id=mid)
    seed.make_card(co, "Q2", "A2", material_id=mid)
    other = seed.make_card(co, "keep", "me")      # different material -> untouched
    body = client_.delete(f"/api/materials/{mid}?cards=1").get_json()
    assert body["deleted_cards"] == 2
    assert db.query_one("SELECT COUNT(*) n FROM cards")["n"] == 1
    assert db.query_one("SELECT id FROM cards WHERE id=?", (other,)) is not None


def test_delete_missing_material_is_not_an_error(client_):
    assert client_.delete("/api/materials/999999").get_json()["ok"] is True
