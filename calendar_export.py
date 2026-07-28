"""Static .ics export of assignment deadlines (calendar_export.py).

A STATIC snapshot (not a live webcal subscription — that needs the Mac awake).
Written to disk / served on demand so deadlines land in the iPhone's built-in
Calendar and fire notifications even when the Mac is asleep = "value without
opening the app". Caveat: deadline changes only show after re-export.
"""

import os
from datetime import timezone

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ICS = os.path.join(BASE_DIR, "exports", "studydash.ics")


def _ics_dt(iso):
    dt = db.parse_iso(iso)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _esc(s):
    if s is None:
        return ""
    return (str(s).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def build_ics():
    now = db.now_utc_iso()
    stamp = _ics_dt(now)
    rows = db.query(
        db.ASSIGN_JOIN + " WHERE a.due_at IS NOT NULL AND a.status IN "
        "('todo','in_progress','proposed') ORDER BY a.due_at")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//StudyDash//JP//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:StudyDash 締切",
    ]
    for a in rows:
        dt = _ics_dt(a["due_at"])
        if not dt:
            continue
        title = a["title"]
        course = a["course_name"] or ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:studydash-{a['id']}@localhost",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{dt}",
            f"DTEND:{dt}",
            f"SUMMARY:{_esc((course + ' ') if course else '')}{_esc(title)}",
            f"DESCRIPTION:{_esc('種別: ' + (a['category'] or ''))}"
            f"{_esc(' / 配点: ' + str(a['max_points']) if a['max_points'] else '')}",
            "BEGIN:VALARM", "TRIGGER:-PT12H", "ACTION:DISPLAY",
            f"DESCRIPTION:{_esc(title)}", "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def write_ics(path=DEFAULT_ICS):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = build_ics()
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


if __name__ == "__main__":
    p = write_ics()
    print("wrote", p)
