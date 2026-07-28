"""Behavior-change tests: D-4 hardening, upload robustness, poison-pill guard,
JSON errors, atomicity — the risky Tier-1 changes."""

import io
import os
import subprocess

import pytest

import ai
import app as app_module
import db
import generate
import ingest
from tests import seed


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


class FakeStorage:
    """Minimal stand-in for a Werkzeug FileStorage (filename/stream/save)."""

    def __init__(self, filename, data):
        self.filename = filename
        self.stream = io.BytesIO(data)

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self.stream.getvalue())


# --------------------------------------------------------------------------
# C2 — D-4 defensive normalization
# --------------------------------------------------------------------------
def test_create_normalizes_offset_to_utc(client):
    r = client.post("/api/assignments", json={
        "title": "オフセット", "due_at": "2026-08-01T23:59:00+09:00"})
    assert r.status_code == 201
    assert r.get_json()["due_at"] == "2026-08-01T14:59:00+00:00"


def test_create_keeps_unparseable_due_never_nulls(client):
    r = client.post("/api/assignments", json={
        "title": "変な締切", "due_at": "not-a-real-date"})
    assert r.status_code == 201
    # a present-but-bad value is preserved, NOT silently dropped to NULL
    assert r.get_json()["due_at"] == "not-a-real-date"


def test_update_normalizes_offset(client):
    aid = seed.make_assignment(title="up")
    r = client.patch(f"/api/assignments/{aid}",
                     json={"due_at": "2026-12-31T10:00:00+05:00"})
    assert r.status_code == 200
    assert r.get_json()["due_at"] == "2026-12-31T05:00:00+00:00"


# --------------------------------------------------------------------------
# C3 — upload robustness
# --------------------------------------------------------------------------
def test_oversized_file_becomes_a_ValueError(monkeypatch):
    monkeypatch.setattr(ingest, "_max_upload_bytes", lambda: 10)
    with pytest.raises(ValueError) as e:
        ingest.process_upload(FakeStorage("huge.jpg", b"x" * 100))
    assert "大きすぎます" in str(e.value)


def test_upload_route_lists_oversize_in_errors(client, monkeypatch):
    monkeypatch.setattr(ingest, "_max_upload_bytes", lambda: 10)
    data = {"files": (io.BytesIO(b"y" * 100), "huge.jpg")}
    r = client.post("/api/upload", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 201
    body = r.get_json()
    assert body["materials"] == []
    assert body["errors"] and "大きすぎます" in body["errors"][0]


def test_heic_conversion_failure_deletes_orphan(monkeypatch, tmp_path):
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setattr(ingest, "UPLOADS_DIR", str(up))

    def boom(*args):
        raise subprocess.CalledProcessError(1, "sips")

    monkeypatch.setattr(ingest, "_sips", boom)
    with pytest.raises(ValueError) as e:
        ingest.process_upload(FakeStorage("pic.heic", b"not-really-heic"))
    assert "変換に失敗" in str(e.value)
    assert os.listdir(str(up)) == []   # no orphan left behind


def test_unsupported_extension_rejected():
    with pytest.raises(ValueError):
        ingest.process_upload(FakeStorage("notes.txt", b"hi"))


# --------------------------------------------------------------------------
# C3 — JSON error handler
# --------------------------------------------------------------------------
def test_404_is_json(client):
    r = client.get("/api/materials/999999")
    assert r.status_code == 404 and r.is_json
    body = r.get_json()
    assert "error" in body and "detail" in body


def test_400_is_json(client):
    r = client.post("/api/assignments", json={"title": ""})
    assert r.status_code == 400 and r.is_json


# --------------------------------------------------------------------------
# C4 — Popen OSError wrap
# --------------------------------------------------------------------------
def test_run_claude_wraps_spawn_oserror(monkeypatch):
    monkeypatch.setattr(ai, "check_claude", lambda: (True, "/bin/true"))

    def boom(*a, **k):
        raise OSError("cannot exec")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(ai.ClaudeError):
        ai.run_claude("hello")


# --------------------------------------------------------------------------
# C4 — poison-pill guard (fake runner; ZERO real claude billing)
# --------------------------------------------------------------------------
def test_poison_pill_caps_claude_and_never_bills_after_N(monkeypatch):
    calls = {"n": 0}

    def crashing_run(*a, **k):
        calls["n"] += 1
        raise RuntimeError("simulated hard crash mid-call")

    monkeypatch.setattr(ai, "run_claude", crashing_run)
    monkeypatch.setattr(db, "max_material_attempts", lambda: 3)

    co = seed.make_course()
    mid = seed.make_material(co, status="generating", extracted_text="t",
                             attempts=0)

    # Emulate the crash/reconcile loop: while the material is still 'generating',
    # keep re-running generate (as reconcile would). A crash re-raises but the
    # attempts bump was already committed, so the material stays 'generating'.
    for _ in range(10):
        cur = db.query_one("SELECT status FROM materials WHERE id=?", (mid,))
        if cur["status"] != "generating":
            break
        try:
            generate.generate_for_material(mid)
        except RuntimeError:
            pass

    assert calls["n"] == 3   # claude spawned at most N times, then guarded
    row = db.query_one("SELECT status, attempts FROM materials WHERE id=?",
                       (mid,))
    assert row["status"] == "failed"
    assert row["attempts"] == 3


def test_attempts_bumped_before_call(monkeypatch):
    """The increment must land BEFORE the claude call, so a crash still counts."""
    seen = {"attempts_at_call": None}

    def run(*a, **k):
        r = db.query_one("SELECT attempts FROM materials WHERE id=?", (mid,))
        seen["attempts_at_call"] = r["attempts"]
        raise ai.ClaudeError("boom")

    monkeypatch.setattr(ai, "run_claude", run)
    co = seed.make_course()
    mid = seed.make_material(co, status="generating", extracted_text="t",
                             attempts=0)
    generate.generate_for_material(mid)
    assert seen["attempts_at_call"] == 1   # already incremented when claude ran


def test_retry_resets_attempts(client):
    co = seed.make_course()
    mid = seed.make_material(co, status="failed", attempts=5)
    r = client.post(f"/api/materials/{mid}/retry")
    assert r.status_code == 200
    row = db.query_one("SELECT status, attempts FROM materials WHERE id=?",
                       (mid,))
    assert row["attempts"] == 0 and row["status"] == "extracting"


def test_regenerate_resets_attempts(client):
    co = seed.make_course()
    mid = seed.make_material(co, status="failed", attempts=5)
    r = client.post(f"/api/materials/{mid}/regenerate")
    assert r.status_code == 200
    assert db.material_attempts(mid) == 0


# --------------------------------------------------------------------------
# E-support — material_dict additive fields for the honest-completion UI
# --------------------------------------------------------------------------
def test_material_dict_reports_card_count_and_exhaustion(client):
    co = seed.make_course()
    mid = seed.make_material(co, status="done", attempts=5)
    seed.make_card(co, "f", "b", material_id=mid)
    got = [m for m in client.get("/api/materials").get_json()
           if m["id"] == mid][0]
    assert got["card_count"] == 1
    assert got["attempts_exhausted"] is True


def test_material_dict_not_exhausted_when_below_cap(client):
    co = seed.make_course()
    mid = seed.make_material(co, status="failed", attempts=1)
    got = [m for m in client.get("/api/materials").get_json()
           if m["id"] == mid][0]
    assert got["attempts_exhausted"] is False
