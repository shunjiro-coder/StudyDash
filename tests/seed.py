"""Row-insertion helpers for tests. Small and explicit — each test seeds exactly
the shape it exercises (the app's own empty branches otherwise hide bugs)."""

import json

import db


def iso(dt):
    return db.to_utc_iso(dt)


def make_course(name="数学", subject_type="stem", weights=None):
    return db.write(
        "INSERT INTO courses (name, subject_type, weight_config_json, created_at) "
        "VALUES (?,?,?,?)",
        (name, subject_type, json.dumps(weights) if weights else None,
         db.now_utc_iso()))


def make_assignment(course_id=None, title="課題", due_at=None,
                    category="homework", status="todo", max_points=None,
                    estimated_minutes=60, pinned=0, source="manual",
                    external_id=None):
    now = db.now_utc_iso()
    return db.write(
        """INSERT INTO assignments
           (course_id, title, due_at, category, max_points, status,
            estimated_minutes, pinned, source, external_id, updated_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (course_id, title, due_at, category, max_points, status,
         estimated_minutes, pinned, source, external_id, now, now))


def make_card(course_id, front, back, state="new", next_due_at=None,
              repetitions=0, current_interval=0, current_ease=2.5,
              confidence=None, origin="generated", verified=None,
              material_id=None, topic=None, card_type="qa"):
    now = db.now_utc_iso()
    return db.write(
        """INSERT INTO cards
           (course_id, material_id, card_type, front, back, topic, origin,
            confidence, verified, content_hash, state, next_due_at, repetitions,
            current_interval, current_ease, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (course_id, material_id, card_type, front, back, topic, origin,
         confidence, verified, db.content_hash(front, back), state, next_due_at,
         repetitions, current_interval, current_ease, now))


def add_review(card_id, grade, reviewed_at=None, interval_days=1,
               ease_factor=2.5):
    return db.write(
        "INSERT INTO reviews (card_id, reviewed_at, grade, interval_days, "
        "ease_factor) VALUES (?,?,?,?,?)",
        (card_id, reviewed_at or db.now_utc_iso(), grade, interval_days,
         ease_factor))


def make_material(course_id=None, kind="photo", status="done",
                  extracted_text="本文", extracted_json=None, attempts=0,
                  original_path="uploads/x.jpg"):
    return db.write(
        """INSERT INTO materials
           (course_id, kind, original_path, status, extracted_text,
            extracted_json, attempts, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (course_id, kind, original_path, status, extracted_text,
         json.dumps(extracted_json) if extracted_json else None, attempts,
         db.now_utc_iso()))


def make_study_guide(course_id, content_md="# まとめ", scope_desc="範囲"):
    return db.write(
        "INSERT INTO study_guides (course_id, scope_desc, content_md, created_at) "
        "VALUES (?,?,?,?)",
        (course_id, scope_desc, content_md, db.now_utc_iso()))


# --- Phase B1: notes/outliner ---------------------------------------------
def make_doc(title="ノート", course_id=None, is_daily=0, daily_date=None,
             archived=0):
    now = db.now_utc_iso()
    return db.write(
        """INSERT INTO docs
           (title, course_id, is_daily, daily_date, archived, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (title, course_id, is_daily, daily_date, archived, now, now))


def make_rem(doc_id, text="", parent_id=None, position=1.0, rem_type="bullet"):
    now = db.now_utc_iso()
    return db.write(
        """INSERT INTO rems
           (doc_id, parent_id, position, text, rem_type, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (doc_id, parent_id, position, text, rem_type, now, now))
