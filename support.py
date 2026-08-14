"""Problem reports from whoever is running this copy.

There is no server to send anything to — this app is deliberately local-only — so a
report is a MARKDOWN FILE the person hands back to the maintainer, who can read it
or paste it straight into Claude Code for diagnosis. The format is written for that
second reader: environment first, then what they were doing, then the evidence.

Privacy is the design constraint. A report carries COUNTS, never content: no card
fronts or backs, no note text, no file names, no transcribed material. The two
things that could contain a fragment of the person's own material — the last
material error and the log tail — are strictly opt-in and named as such in the UI.
"""

import os
import platform
import re
import sys

import db
import version

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDBACK_DIR = os.path.join(BASE_DIR, "feedback")
LOG_PATH = os.path.join(BASE_DIR, "studydash.log")

KINDS = {
    "bug": "うまく動かない / something is broken",
    "confusing": "使い方が分かりにくい / confusing to use",
    "idea": "こうしてほしい / a request",
    "other": "その他 / other",
}

LOG_TAIL_LINES = 40
MESSAGE_MAX = 4000

# A log line can quote a path or a filename from the person's own machine. Reports
# are meant to be shareable, so those are blanked before the tail is included.
_HOME_RE = re.compile(r"(/Users/|/home/|C:\\Users\\)[^/\\ \"']+", re.I)
_UPLOAD_RE = re.compile(r"uploads[/\\][\w.-]+", re.I)


def _scrub(text):
    text = _HOME_RE.sub(r"\1<user>", text or "")
    return _UPLOAD_RE.sub("uploads/<file>", text)


def counts():
    """Shape of the library, with no trace of what is in it."""
    def n(sql):
        try:
            return db.query_one(sql)["n"]
        except Exception:  # noqa: BLE001 — a report must never fail to build
            return -1
    return {
        "courses": n("SELECT COUNT(*) n FROM courses"),
        "materials": n("SELECT COUNT(*) n FROM materials"),
        "cards": n("SELECT COUNT(*) n FROM cards"),
        "cards_new": n("SELECT COUNT(*) n FROM cards WHERE state='new'"),
        "cards_review": n("SELECT COUNT(*) n FROM cards WHERE state='review'"),
        "assignments": n("SELECT COUNT(*) n FROM assignments"),
        "notes": n("SELECT COUNT(*) n FROM rems"),
    }


def environment():
    import ai
    ok_claude, _ = ai.check_claude()
    try:
        # ordered by when they ran; the table is keyed by version, with no rowid
        # column to sort on (an ORDER BY id here silently emptied this list).
        applied = [r["version"] for r in db.query(
            "SELECT version FROM schema_migrations ORDER BY applied_at, version")]
    except Exception:  # noqa: BLE001 — pre-migration DBs have no ledger
        applied = []
    return {
        "app_version": version.__version__,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "claude_cli": bool(ok_claude),
        "migrations": applied,
    }


def last_material_error():
    """Opt-in: the newest failure the ingest/generate pipeline recorded."""
    try:
        r = db.query_one(
            "SELECT id, status, error_message, attempts FROM materials "
            "WHERE error_message IS NOT NULL AND error_message != '' "
            "ORDER BY id DESC LIMIT 1")
    except Exception:  # noqa: BLE001
        return None
    if not r:
        return None
    return {"material_id": r["id"], "status": r["status"],
            "attempts": r["attempts"], "error": _scrub(r["error_message"])[:800]}


def log_tail(lines=LOG_TAIL_LINES):
    """Opt-in: the end of the server log, with local paths blanked."""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-lines:]
    except OSError:
        return None
    return _scrub("".join(tail)).strip() or None


def build_report(kind, message, include_error=False, include_log=False):
    """Returns (report_markdown, payload). Never raises on a partial environment."""
    kind = kind if kind in KINDS else "other"
    message = db.text_cell(message)[:MESSAGE_MAX]
    env, cnt = environment(), counts()
    err = last_material_error() if include_error else None
    log = log_tail() if include_log else None

    L = []
    L.append("# StudyDash 不具合レポート / problem report")
    L.append("")
    L.append("> このファイルを開発者に送ってください。開発者はこれをそのまま "
             "Claude Code に渡して原因を調べられます。")
    L.append("> *Send this file to the maintainer; it can be handed to Claude Code "
             "as-is to diagnose.*")
    L.append("")
    L.append("## 種類 / kind")
    L.append(KINDS[kind])
    L.append("")
    L.append("## 起きたこと / what happened")
    L.append(message or "(記入なし / not filled in)")
    L.append("")
    L.append("## 環境 / environment")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append("| StudyDash | v%s |" % env["app_version"])
    L.append("| OS | %s |" % env["platform"])
    L.append("| Python | %s |" % env["python"])
    L.append("| claude CLI | %s |" % ("あり / present" if env["claude_cli"]
                                      else "なし / absent"))
    L.append("| migrations | %s |" % (", ".join(env["migrations"]) or "(none)"))
    L.append("")
    L.append("## データ量 / library size")
    L.append("*内容は含みません。件数のみ。 Counts only — no card, note or file "
             "content is included.*")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    for k, v in cnt.items():
        L.append("| %s | %s |" % (k, v))
    L.append("")
    if err:
        L.append("## 直近のエラー / last recorded error")
        L.append("")
        L.append("- material #%s (status `%s`, attempts %s)"
                 % (err["material_id"], err["status"], err["attempts"]))
        L.append("")
        L.append("```")
        L.append(err["error"])
        L.append("```")
        L.append("")
    if log:
        L.append("## ログ末尾 / log tail")
        L.append("")
        L.append("```")
        L.append(log)
        L.append("```")
        L.append("")
    L.append("---")
    L.append("*報告日時 / reported: %s (UTC)*" % db.now_utc_iso())
    md = "\n".join(L)
    return md, {"kind": kind, "message": message, "environment": env,
                "counts": cnt, "error": err, "log_included": bool(log)}


def save_report(kind, message, include_error=False, include_log=False):
    """Write the report to feedback/ and record it. Returns the stored row."""
    md, _ = build_report(kind, message, include_error, include_log)
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    stamp = db.now_utc_iso().replace(":", "").replace("-", "")[:15]
    name = "report-%s.md" % stamp
    path = os.path.join(FEEDBACK_DIR, name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        rel = os.path.relpath(path, BASE_DIR)
    except OSError:
        rel = None   # unwritable disk must not lose the report — the DB still has it
    rid = db.write(
        "INSERT INTO feedback_reports (kind, message, report_md, file_path, "
        "created_at) VALUES (?,?,?,?,?)",
        (kind, message, md, rel, db.now_utc_iso()))
    return {"id": rid, "kind": kind, "file_path": rel, "report_md": md}
