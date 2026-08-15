"""The Windows paths, exercised from macOS.

Windows is a promised platform with no machine here to try it on, so the parts
that CAN be tested without one are: the branches keyed off os.name, the packaged
launchers' line endings, and the graceful degradation where a macOS-only tool is
missing. Everything here would silently pass on a Mac before Phase M — that is
the point.
"""

import os
import pathlib
import subprocess

import pytest

import ai
import ingest

REPO = pathlib.Path(__file__).resolve().parent.parent


# -------------------- spawning claude --------------------
def test_cmd_shim_is_routed_through_the_command_processor(monkeypatch):
    """A .cmd is a script, not a PE image: CreateProcess cannot exec it directly,
    so Popen must be handed cmd.exe /c. Without this, every AI call on Windows
    dies at spawn."""
    monkeypatch.setattr(ai, "IS_WINDOWS", True)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    argv = ai._spawn_cmd([r"C:\claude\claude.cmd", "-p", "hi"])
    assert argv[:2] == [r"C:\Windows\System32\cmd.exe", "/c"]
    assert argv[2:] == [r"C:\claude\claude.cmd", "-p", "hi"]


@pytest.mark.parametrize("exe", ["claude.CMD", "claude.bat"])
def test_shim_detection_is_case_insensitive(monkeypatch, exe):
    monkeypatch.setattr(ai, "IS_WINDOWS", True)
    assert ai._spawn_cmd([exe])[1] == "/c"


def test_real_binaries_are_not_wrapped(monkeypatch):
    monkeypatch.setattr(ai, "IS_WINDOWS", True)
    argv = [r"C:\claude\claude.exe", "-p"]
    assert ai._spawn_cmd(argv) == argv


def test_posix_never_wraps(monkeypatch):
    monkeypatch.setattr(ai, "IS_WINDOWS", False)
    argv = ["/usr/local/bin/claude", "-p"]
    assert ai._spawn_cmd(argv) == argv


# -------------------- killing a timed-out call --------------------
class _FakeProc:
    def __init__(self):
        self.killed = False
        self.pid = 4242

    def kill(self):
        self.killed = True


def test_timeout_kill_uses_kill_on_windows(monkeypatch):
    """os.killpg does not exist on Windows. It used to be called unconditionally,
    and the resulting AttributeError escaped the caller's
    (ProcessLookupError, PermissionError) guard — taking down the ingest worker
    instead of failing the one call."""
    monkeypatch.setattr(ai, "IS_WINDOWS", True)
    monkeypatch.delattr(os, "killpg", raising=False)
    proc = _FakeProc()
    ai._kill_tree(proc)          # must not raise
    assert proc.killed


def test_timeout_kill_never_raises_when_the_process_is_already_gone(monkeypatch):
    monkeypatch.setattr(ai, "IS_WINDOWS", False)

    def boom(*a):
        raise ProcessLookupError("already reaped")
    monkeypatch.setattr(os, "getpgid", boom)
    ai._kill_tree(_FakeProc())   # must not raise


def test_posix_spawn_passes_no_windows_only_flags(monkeypatch):
    """subprocess.CREATE_NEW_PROCESS_GROUP does not exist off Windows, so the
    kwargs must stay behind a lazily-evaluated guard: referencing it eagerly would
    make every AI call on macOS/Linux die at spawn with AttributeError."""
    captured = {}

    class FakeProc:
        pid = 999999
        returncode = 0

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("claude", timeout or 1)

        def kill(self):
            pass

    def fake_popen(argv, **kw):
        captured.update(kw)
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(ai, "IS_WINDOWS", False)
    monkeypatch.setattr(ai, "check_claude", lambda: (True, "/bin/echo"))
    monkeypatch.setattr(ai.subprocess, "Popen", fake_popen)

    with pytest.raises(ai.ClaudeError):        # the fake always times out
        ai.run_claude("hi", timeout=1)

    assert "creationflags" not in captured, "Windows-only flag leaked onto POSIX"
    assert captured["start_new_session"] is True
    assert captured["argv"][0] == "/bin/echo"  # not wrapped in cmd.exe


# -------------------- HEIC without sips --------------------
def test_heic_without_sips_explains_what_to_do(monkeypatch, tmp_path):
    """sips is macOS-only and we ship no image library, so the failure has to name
    the fix rather than say 'conversion failed'."""
    monkeypatch.setattr(ingest, "_has_sips", lambda: False)
    with pytest.raises(ValueError) as exc:
        ingest._postprocess(str(tmp_path / "x.heic"), ".heic", "photo", "x")
    msg = str(exc.value)
    assert "JPG" in msg and "HEIC" in msg
    assert "macOS" in msg or "Mac" in msg


def test_photo_resize_is_skipped_not_failed_without_sips(monkeypatch, tmp_path):
    """Downscaling is an optimization; its absence must not block an upload."""
    monkeypatch.setattr(ingest, "_has_sips", lambda: False)
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"not-really-a-jpeg")
    assert ingest._postprocess(str(src), ".jpg", "photo", "photo") == str(src)


# -------------------- what actually ships --------------------
@pytest.mark.parametrize("name", ["Start-Windows.bat", "Update-Windows.bat"])
def test_batch_files_use_crlf(name):
    """cmd.exe implements `goto` by seeking through the file; with LF-only endings
    that seek lands mid-line and the label jump fails."""
    data = (REPO / name).read_bytes()
    assert data.count(b"\n") > 0
    assert data.count(b"\r\n") == data.count(b"\n"), "found bare LF endings"
    assert not data.startswith(b"\xef\xbb\xbf"), "a UTF-8 BOM breaks the first line"


@pytest.mark.parametrize("name", ["Start-Mac.command", "Update-Mac.command"])
def test_mac_launchers_use_lf(name):
    """A CR would ride along on the shebang and every command -> 'command not found'."""
    data = (REPO / name).read_bytes()
    assert b"\r" not in data
    assert data.startswith(b"#!/bin/bash")


@pytest.mark.parametrize("name", ["Start-Windows.bat", "Update-Windows.bat"])
def test_batch_labels_are_all_defined(name):
    """A goto with no matching label aborts the script on Windows only."""
    text = (REPO / name).read_text(encoding="utf-8")
    labels = {ln.strip()[1:].lower() for ln in text.splitlines()
              if ln.strip().startswith(":") and not ln.strip().startswith("::")}
    for line in text.splitlines():
        parts = line.strip().lower().split("goto ")
        if len(parts) > 1:
            target = parts[1].strip().lstrip(":").split()[0]
            assert target in labels or target == "eof", \
                "%s: goto %s has no label" % (name, target)
