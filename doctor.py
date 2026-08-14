"""Environment check for people who did not build this app.

Run standalone (`python3 doctor.py`) BEFORE the venv exists, so it must import
nothing outside the standard library and must work on Python 3.7+ even though the
app itself wants 3.9+ — a version error has to be reportable, not a SyntaxError.

Every line is bilingual: the recipients read Japanese, the tracebacks are English.
"""

import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OK, WARN, BAD = "OK", "!!", "XX"

MIN_PY = (3, 9)


def _c(status, ja, en, fix=None):
    return {"status": status, "ja": ja, "en": en, "fix": fix}


def check_python():
    v = sys.version_info
    got = "%d.%d.%d" % (v.major, v.minor, v.micro)
    if v[:2] >= MIN_PY:
        return _c(OK, "Python %s" % got, "Python %s" % got)
    return _c(
        BAD,
        "Python %s は古すぎます（3.9 以上が必要）" % got,
        "Python %s is too old (3.9+ required)" % got,
        "https://www.python.org/downloads/ から最新版を入れてください / install from python.org",
    )


def check_venv():
    exe = os.path.join(BASE_DIR, "venv",
                       "Scripts" if os.name == "nt" else "bin",
                       "python.exe" if os.name == "nt" else "python")
    if os.path.exists(exe):
        return _c(OK, "仮想環境（venv）あり", "virtual environment present")
    return _c(WARN, "仮想環境（venv）は未作成", "virtual environment not created yet",
              "起動スクリプトが自動で作ります / the start script creates it automatically")


def check_flask():
    try:
        import flask  # noqa: F401
    except ImportError:
        return _c(WARN, "Flask 未インストール", "Flask not installed",
                  "起動スクリプトが自動で入れます / the start script installs it")
    return _c(OK, "Flask あり", "Flask present")


def _claude_override():
    """The app resolves `claude_bin` from settings before falling back to PATH; read
    the same key here (by hand, no db import — doctor runs before deps exist) so a
    working install outside PATH isn't reported as missing."""
    import json
    for name in ("settings.local.json", "settings.json"):
        try:
            with open(os.path.join(BASE_DIR, name), encoding="utf-8") as fh:
                got = (json.load(fh) or {}).get("claude_bin")
        except (OSError, ValueError):
            continue
        if got:
            got = os.path.expanduser(got)
            if os.path.exists(got):
                return got
    return None


def check_claude():
    """AI is optional: without it StudyDash still runs as a manual SRS + notes app."""
    found = _claude_override() or shutil.which("claude")
    if not found:
        return _c(WARN, "claude CLI が見つかりません（AI機能はオフ・他は全部使えます）",
                  "claude CLI not found (AI features off; everything else works)",
                  "https://claude.com/claude-code を入れてから "
                  "`claude login` / install Claude Code, then run `claude login`")
    try:
        p = subprocess.run([found, "--version"], capture_output=True, text=True,
                           timeout=20)
        ver = (p.stdout or p.stderr or "").strip().splitlines()[:1]
        ver = ver[0] if ver else "?"
    except (OSError, subprocess.SubprocessError):
        return _c(WARN, "claude はあるが実行できませんでした",
                  "claude found but could not be executed",
                  "`claude login` を一度実行してください / run `claude login` once")
    return _c(OK, "claude CLI あり（%s）" % ver, "claude CLI present (%s)" % ver)


def check_sips():
    """HEIC conversion + photo downscaling. macOS-only; absence is not an error."""
    if shutil.which("sips"):
        return _c(OK, "画像変換（sips）あり", "image conversion (sips) present")
    return _c(WARN, "HEIC は使えません（JPG/PNG/PDF は問題なし）",
              "HEIC unsupported here (JPG/PNG/PDF are fine)",
              "iPhone の写真は JPG で保存して取り込んでください / "
              "save iPhone photos as JPG before uploading")


def check_data():
    db_path = os.environ.get("STUDYDASH_DB") or os.path.join(BASE_DIR, "study.db")
    if os.path.exists(db_path):
        mb = os.path.getsize(db_path) / (1024 * 1024)
        return _c(OK, "学習データあり（%.1fMB）" % mb, "study data present (%.1fMB)" % mb)
    return _c(WARN, "学習データはまだありません（初回起動で作られます）",
              "no study data yet (created on first launch)")


CHECKS = (
    ("Python", check_python),
    ("venv", check_venv),
    ("Flask", check_flask),
    ("Claude", check_claude),
    ("画像/Images", check_sips),
    ("データ/Data", check_data),
)


def run():
    """Returns (results, fatal). fatal=True means the app cannot start at all."""
    results, fatal = [], False
    for name, fn in CHECKS:
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001 — a broken check must not block startup
            r = _c(WARN, "確認できませんでした: %s" % e, "could not check: %s" % e)
        r["name"] = name
        results.append(r)
        if r["status"] == BAD:
            fatal = True
    return results, fatal


def main():
    try:
        import version
        ver = version.__version__
    except Exception:  # noqa: BLE001 — version is cosmetic
        ver = "?"
    print("")
    print("  StudyDash  v%s  —  環境チェック / environment check" % ver)
    print("  " + "-" * 56)
    results, fatal = run()
    for r in results:
        mark = {OK: "  OK  ", WARN: "  --  ", BAD: "  XX  "}[r["status"]]
        print("%s%-12s %s" % (mark, r["name"], r["ja"]))
        print("%s%-12s %s" % (" " * 6, "", r["en"]))
        if r["fix"]:
            print("%s%-12s -> %s" % (" " * 6, "", r["fix"]))
    print("  " + "-" * 56)
    if fatal:
        print("  起動できません。上の XX を直してください。")
        print("  Cannot start. Fix the XX item(s) above.")
    else:
        print("  起動できます。 / Ready to start.")
    print("")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
