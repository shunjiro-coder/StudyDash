"""Phase N: problem reports.

The load-bearing property is privacy: a report is meant to be forwarded to someone
else, so it must carry counts and environment, never the person's study content.
"""

import json
import os

import pytest

import app as app_module
import db
import support
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def test_feedback_table_exists():
    tables = {r["name"] for r in db.get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "feedback_reports" in tables


# -------------------- privacy --------------------
def test_report_never_contains_card_or_note_content(clock):
    co = seed.make_course()
    secret_front = "ZZTOPSECRETFRONT"
    secret_back = "ZZTOPSECRETBACK"
    seed.make_card(co, secret_front, secret_back)
    db.write("INSERT INTO docs (title, created_at, updated_at) VALUES (?,?,?)",
             ("ZZSECRETDOC", db.now_utc_iso(), db.now_utc_iso()))
    md, _ = support.build_report("bug", "カードが出ません",
                                 include_error=True, include_log=True)
    for secret in (secret_front, secret_back, "ZZSECRETDOC"):
        assert secret not in md
    assert "cards" in md            # the COUNT is there
    assert "カードが出ません" in md   # the person's own words are kept


def test_scrub_blanks_home_paths_and_upload_names():
    dirty = ("/Users/someone/Desktop/thing.pdf failed; "
             "uploads/9f8a7b6c5d.jpg unreadable")
    clean = support._scrub(dirty)
    assert "someone" not in clean
    assert "9f8a7b6c5d" not in clean
    assert "uploads/<file>" in clean


def test_error_and_log_are_opt_in(clock):
    co = seed.make_course()
    mid = seed.make_material(co)
    db.write("UPDATE materials SET error_message=? WHERE id=?",
             ("BOOMDISTINCTIVE", mid))
    off, _ = support.build_report("bug", "x", include_error=False)
    on, _ = support.build_report("bug", "x", include_error=True)
    assert "BOOMDISTINCTIVE" not in off
    assert "BOOMDISTINCTIVE" in on


# -------------------- content --------------------
def test_environment_actually_lists_applied_migrations(clock):
    """Regression: this query used to ORDER BY a column the table does not have,
    so the guard swallowed the error and every report claimed zero migrations."""
    env = support.environment()
    assert env["migrations"], "migration list came back empty"
    assert "n_feedback" in env["migrations"]
    assert env["app_version"] and env["python"]


def test_report_names_its_second_reader(clock):
    md, _ = support.build_report("bug", "壊れました")
    assert "Claude Code" in md          # the maintainer pastes it there
    assert "StudyDash" in md


def test_build_report_survives_a_bare_environment(clock, monkeypatch):
    """A report about a broken app must not itself fail to build."""
    monkeypatch.setattr(support, "counts", lambda: {"cards": -1})
    md, payload = support.build_report("other", "何かおかしい")
    assert "何かおかしい" in md and payload["kind"] == "other"


def test_unknown_kind_falls_back_to_other(clock):
    _, payload = support.build_report("../etc/passwd", "hi")
    assert payload["kind"] == "other"


def test_message_is_capped(clock):
    md, payload = support.build_report("bug", "あ" * 9000)
    assert len(payload["message"]) == support.MESSAGE_MAX


# -------------------- endpoint --------------------
def test_post_saves_a_report_and_lists_it(client, clock, tmp_path, monkeypatch):
    monkeypatch.setattr(support, "FEEDBACK_DIR", str(tmp_path / "feedback"))
    res = client.post("/api/feedback", json={"kind": "idea",
                                             "message": "ダークモードを濃くしてほしい"})
    body = res.get_json()
    assert body["ok"] and body["id"] > 0
    assert "ダークモードを濃くしてほしい" in body["report_md"]
    assert body["file_path"].endswith(".md")

    listed = client.get("/api/feedback").get_json()
    assert listed["reports"][0]["kind"] == "idea"
    assert "bug" in listed["kinds"]


def test_post_requires_a_message(client, clock):
    body = client.post("/api/feedback", json={"kind": "bug", "message": "   "}).get_json()
    assert body["ok"] is False and body["error"]


def test_post_never_500s_when_saving_breaks(client, clock, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk on fire")
    monkeypatch.setattr(support, "save_report", boom)
    res = client.post("/api/feedback", json={"kind": "bug", "message": "help"})
    assert res.status_code == 200 and res.get_json()["ok"] is False


def test_non_string_message_does_not_crash(client, clock):
    res = client.post("/api/feedback", json={"kind": "bug", "message": {"a": 1}})
    assert res.status_code == 200
    assert res.get_json()["ok"] is False   # text_cell maps a dict to "" -> rejected


def test_report_json_payload_shape(clock):
    _, payload = support.build_report("bug", "x")
    assert set(payload) == {"kind", "message", "environment", "counts",
                            "error", "log_included"}
    assert json.dumps(payload)   # must stay JSON-serializable for the endpoint


# -------------------- doctor / app agreement --------------------
def test_doctor_resolves_claude_the_same_way_the_app_does():
    """Regression: doctor checked only PATH while ai.resolve_claude also falls back
    to ~/.local/bin — where claude actually installs. A recipient with a working
    install was told the AI was unavailable while the app was using it fine."""
    import ai
    import doctor
    app_path = ai.resolve_claude()
    doc_path = doctor._resolve_claude()
    if doc_path is None:
        assert not os.path.isfile(app_path)   # neither found it: consistent
    else:
        assert os.path.realpath(doc_path) == os.path.realpath(app_path)
