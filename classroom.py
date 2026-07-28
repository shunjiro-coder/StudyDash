"""Google Classroom client (optional data source).

Two-stage design: if Classroom can't be reached (school account blocks
third-party apps, or no OAuth client is set up yet), the whole app still works
on manual input — assignments carry a `source` column so nothing here is load-
bearing. `python classroom.py --test` decides black/white in ~15 minutes.

OAuth pitfalls handled (per plan):
- run_local_server(port=0)  (run_console is removed by Google)
- access_type='offline', prompt='consent'  (else refresh token isn't returned)
- token.json holds the refresh token -> caller chmod 600s it
- Test-published consent screens expire refresh tokens in 7 days (re-consent).
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import db  # first: sets SSL_CERT_FILE via certifi before google libs load

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
]

# Classroom workType -> our category (D-1)
WORKTYPE_CATEGORY = {
    "ASSIGNMENT": "homework",
    "SHORT_ANSWER_QUESTION": "quiz",
    "MULTIPLE_CHOICE_QUESTION": "quiz",
}


class ClassroomUnavailable(Exception):
    """Raised (with a clear, human message) when Classroom can't be used."""


def is_configured():
    return os.path.exists(CREDENTIALS_PATH)


def _load_google():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        return Credentials, InstalledAppFlow, Request, build
    except ImportError as e:  # pragma: no cover
        raise ClassroomUnavailable(
            f"Google client libraries not installed: {e}")


def get_credentials(interactive=True):
    """Return valid OAuth credentials, refreshing or running the consent flow."""
    Credentials, InstalledAppFlow, Request, _ = _load_google()
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception:
            creds = None  # fall through to full consent
    if not is_configured():
        raise ClassroomUnavailable(
            "credentials.json not found. Classroom is optional — set up a "
            "Google Cloud OAuth client and drop credentials.json here, or just "
            "keep using manual input / photo import.")
    if not interactive:
        raise ClassroomUnavailable(
            "No valid token and not interactive. Run `python classroom.py "
            "--test` once to complete the consent flow in a browser.")
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent")
    _save_token(creds)
    return creds


def _save_token(creds):
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def get_service(interactive=True):
    _, _, _, build = _load_google()
    creds = get_credentials(interactive=interactive)
    return build("classroom", "v1", credentials=creds, cache_discovery=False)


def list_courses(interactive=True):
    service = get_service(interactive=interactive)
    resp = service.courses().list(
        courseStates=["ACTIVE"], pageSize=50).execute()
    return resp.get("courses", [])


# --------------------------------------------------------------------------
# Due date -> ISO8601 UTC (D-4). Classroom gives dueDate (UTC date) + dueTime.
# --------------------------------------------------------------------------
def _due_to_iso(cw):
    d = cw.get("dueDate")
    if not d:
        return None
    t = cw.get("dueTime") or {}
    try:
        dt = datetime(
            d["year"], d["month"], d["day"],
            t.get("hours", 23), t.get("minutes", 59), t.get("seconds", 0),
            tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    return db.to_utc_iso(dt)


def _submission_status(subs):
    """Map the student's submission state to our status."""
    if not subs:
        return "todo"
    s = subs[0]
    state = s.get("state")
    if s.get("assignedGrade") is not None and state in ("RETURNED", "TURNED_IN"):
        return "graded"
    if state == "TURNED_IN":
        return "submitted"
    return "todo"


def _submission_grade(subs):
    if subs and subs[0].get("assignedGrade") is not None:
        return subs[0]["assignedGrade"]
    return None


def sync(interactive=False):
    """Pull courses + coursework + my submissions; UPSERT into assignments.

    Returns (courses_seen, assignments_upserted). Idempotent: re-running does
    NOT create duplicates (ON CONFLICT(source, external_id)).
    """
    service = get_service(interactive=interactive)
    courses = service.courses().list(
        courseStates=["ACTIVE"], pageSize=50).execute().get("courses", [])
    now = db.now_utc_iso()
    upserts = 0
    for c in courses:
        course_id = db.get_or_create_course(c.get("name", "Classroom"))
        cw_resp = service.courses().courseWork().list(
            courseId=c["id"], pageSize=100).execute()
        for cw in cw_resp.get("courseWork", []):
            due = _due_to_iso(cw)
            category = WORKTYPE_CATEGORY.get(cw.get("workType"), "homework")
            subs = service.courses().courseWork().studentSubmissions().list(
                courseId=c["id"], courseWorkId=cw["id"], userId="me"
            ).execute().get("studentSubmissions", [])
            status = _submission_status(subs)
            grade = _submission_grade(subs)
            db.write(
                """
                INSERT INTO assignments
                    (course_id, title, description, due_at, category,
                     max_points, earned_points, status, source, external_id,
                     updated_at, last_synced_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'classroom', ?, ?, ?, ?)
                ON CONFLICT(source, external_id) DO UPDATE SET
                    title          = excluded.title,
                    description    = excluded.description,
                    due_at         = excluded.due_at,
                    category       = excluded.category,
                    max_points     = excluded.max_points,
                    earned_points  = excluded.earned_points,
                    status         = excluded.status,
                    updated_at     = excluded.updated_at,
                    last_synced_at = excluded.last_synced_at
                """,
                (course_id, cw.get("title", "(no title)"),
                 cw.get("description"), due, category,
                 cw.get("maxPoints"), grade, status, cw["id"],
                 now, now, now),
            )
            upserts += 1
    return len(courses), upserts


def _cli_test():
    if not is_configured():
        print("[classroom] credentials.json NOT found at", CREDENTIALS_PATH)
        print("            Classroom is optional. The app runs fully on manual")
        print("            input + photo import. To enable Classroom later, add")
        print("            a Google Cloud OAuth client as credentials.json.")
        return 2
    try:
        courses = list_courses(interactive=True)
    except ClassroomUnavailable as e:
        print("[classroom] unavailable:", e)
        return 3
    except Exception as e:  # noqa: BLE001 — surface the real error clearly
        print("[classroom] connection FAILED:", type(e).__name__, str(e)[:400])
        print("            If this is an admin block, keep using manual input.")
        return 1
    print(f"[classroom] OK — {len(courses)} active course(s):")
    for c in courses:
        print("   -", c.get("name"), f"(id={c.get('id')})")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="StudyDash Classroom client")
    ap.add_argument("--test", action="store_true",
                    help="test the connection (lists courses or a clear error)")
    ap.add_argument("--sync", action="store_true",
                    help="run a sync (UPSERT coursework into assignments)")
    args = ap.parse_args()
    if args.sync:
        seen, n = sync(interactive=True)
        print(f"[classroom] synced {seen} course(s), {n} assignment(s) upserted")
        sys.exit(0)
    sys.exit(_cli_test())
