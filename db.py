"""SQLite schema + connection + shared conventions for StudyDash.

Lowest layer: imported by everyone, imports nothing from the app.

Fixed conventions (must not change once data exists):
- D-4 UTC storage: all *_at columns hold ISO8601 UTC strings (now_utc_iso()).
  Local conversion happens ONLY at display time (to_local()).
- D-5 content_hash: sha256(norm(front) + US + norm(back)); NFKC + strip +
  collapse whitespace, NO lowercasing (preserve meaning).

Concurrency:
- SQLite threadsafety==1 -> connections are NOT shared across threads.
  Each thread gets its own connection (thread-local).
- WAL + busy_timeout=5000 set per connection.
- All writes go through a single process-wide lock (_write_lock) so writes are
  serialized (prevents 'database is locked'); reads run lock-free under WAL.
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DB path is overridable via env so tests never touch the real study.db (C11).
DB_PATH = os.environ.get("STUDYDASH_DB") or os.path.join(BASE_DIR, "study.db")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

# SSL: the macOS python.org 3.9 build ships without installed root certs
# (Install Certificates.command needs admin), so raw stdlib https fails.
# Pointing at certifi's bundle fixes every HTTP path (stdlib + requests +
# google libs) for this process without touching system dirs. Imported by
# every module, so this runs before any network call.
try:
    import certifi as _certifi
    os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
except Exception:
    pass

_local = threading.local()
_write_lock = threading.RLock()


# --------------------------------------------------------------------------
# Connection (thread-local)
# --------------------------------------------------------------------------
def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _local.conn = conn
    return conn


def query(sql, params=()):
    return get_conn().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_conn().execute(sql, params).fetchone()


def write(sql, params=()):
    """Single write. Returns lastrowid. Serialized process-wide."""
    conn = get_conn()
    with _write_lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def write_returning(sql, params=()):
    """Single write, returns the cursor (for rowcount etc.). Serialized."""
    conn = get_conn()
    with _write_lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def write_many(statements):
    """statements = iterable of (sql, params). Atomic + serialized."""
    conn = get_conn()
    with _write_lock:
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# --------------------------------------------------------------------------
# D-4  UTC time helpers
# --------------------------------------------------------------------------
def now_dt():
    """The single datetime chokepoint (UTC, tz-aware).

    Every "current time" in the app flows through here — `now_utc_iso()` below,
    plus srs.py / priority.py which need real `datetime` for timedelta/date math.
    Tests freeze THIS one function to make time-dependent logic deterministic.
    """
    return datetime.now(timezone.utc)


def now_utc_iso():
    return now_dt().isoformat()


def to_utc_iso(dt):
    """Normalize any datetime to an ISO8601 UTC string."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s):
    """Parse a stored ISO8601 string (handles a trailing 'Z' for 3.9)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_local(s):
    """Display-only: convert a stored UTC ISO string to local-tz datetime."""
    dt = parse_iso(s)
    return dt.astimezone() if dt else None


def normalize_due(s):
    """Defensive D-4 normalization for an incoming due_at string.

    The UI already sends UTC/Z, but a non-UI client could POST e.g. '+09:00'.
    Normalize any parseable offset to canonical UTC ISO. If the value is absent
    OR unparseable, return it UNCHANGED — never silently NULL a deadline the
    user set (raw normalize would either type-error or drop it)."""
    if not s:
        return None
    dt = parse_iso(s)
    if dt is None:
        return s  # keep the original (bad-but-present) value rather than lose it
    return to_utc_iso(dt)


# --------------------------------------------------------------------------
# D-5  content_hash
# --------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def norm_text(s):
    s = unicodedata.normalize("NFKC", s or "").strip()
    return _WS.sub(" ", s)


def content_hash(front, back):
    payload = norm_text(front) + "\x1f" + norm_text(back)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# E5  source-location: find a verbatim quote inside a transcription so the
# viewer can highlight WHERE a card came from.
# --------------------------------------------------------------------------
def locate(quote, text):
    """Find `quote` within `text`. Matching is NFKC + whitespace-tolerant, but
    char_start/char_end index into the ORIGINAL `text` (what the UI renders);
    both are null when the quote can't be found (the quote is still returned)."""
    q = (quote or "").strip()
    loc = {"quote": q, "char_start": None, "char_end": None}
    if not q or not text:
        return loc
    # normalized copy of `text` + a map from each normalized char back to its
    # original index (NFKC can expand one original char into several).
    norm_chars, idx_map = [], []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_space:
                norm_chars.append(" ")
                idx_map.append(i)
                prev_space = True
            continue
        prev_space = False
        for c in unicodedata.normalize("NFKC", ch):
            norm_chars.append(c)
            idx_map.append(i)
    ntext = "".join(norm_chars)
    nquote = _WS.sub(" ", unicodedata.normalize("NFKC", q)).strip()
    if not nquote:
        return loc
    pos = ntext.find(nquote)
    match_len = len(nquote)
    if pos < 0:  # relax: match a leading slice (tail drift / trailing ellipsis)
        head = nquote[:12]
        if len(head) < 6:
            return loc
        pos = ntext.find(head)
        if pos < 0:
            return loc
        match_len = len(head)
    loc["char_start"] = idx_map[pos]
    loc["char_end"] = idx_map[pos + match_len - 1] + 1
    return loc


# --------------------------------------------------------------------------
# Settings + subject-type inference (shared)
# --------------------------------------------------------------------------
def _local_settings_path():
    # Runtime overrides live in a sibling settings.local.json (untracked). Kept
    # separate so app-written settings never churn the hand-formatted, tracked
    # settings.json defaults.
    base, ext = os.path.splitext(SETTINGS_PATH)
    return base + ".local" + ext


def load_settings():
    """Tracked defaults (settings.json) with runtime overrides (settings.local.json)
    merged on top."""
    s = {}
    for p in (SETTINGS_PATH, _local_settings_path()):
        try:
            with open(p, "r", encoding="utf-8") as f:
                s.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return s


def save_settings(patch):
    """Persist runtime overrides to settings.local.json (untracked), atomically,
    leaving the tracked settings.json pristine. Returns the merged settings."""
    p = _local_settings_path()
    with _write_lock:
        cur = {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        cur.update(patch)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    return load_settings()


# Output language for AI-generated STUDY CONTENT (cards / quiz / summary). 'auto'
# follows the material's language; 'ja'/'en' force it (enables translation-style
# study, e.g. English material -> Japanese cards). The UI chrome stays Japanese.
VALID_CONTENT_LANGS = ("auto", "ja", "en")


def content_lang():
    v = load_settings().get("content_lang", "auto")
    return v if v in VALID_CONTENT_LANGS else "auto"


def infer_subject_type(name):
    """Guess 'stem'|'memo'|'lang'|'other' from a course name; default 'memo'."""
    s = load_settings()
    kw = s.get("subject_keywords", {})
    low = (name or "").lower()
    for stype, words in kw.items():
        for w in words:
            if w and w.lower() in low:
                return stype
    return s.get("default_subject_type", "memo")


# Shared join + serializers (one source of truth for app.py and priority.py)
ASSIGN_JOIN = (
    "SELECT a.*, c.name AS course_name, c.subject_type AS subject_type "
    "FROM assignments a LEFT JOIN courses c ON c.id = a.course_id")


def assignment_dict(r, extra=None):
    keys = r.keys()
    d = {
        "id": r["id"], "course_id": r["course_id"],
        "course_name": r["course_name"] if "course_name" in keys else None,
        "subject_type": r["subject_type"] if "subject_type" in keys else None,
        "title": r["title"], "description": r["description"],
        "due_at": r["due_at"], "category": r["category"],
        "max_points": r["max_points"], "earned_points": r["earned_points"],
        "status": r["status"], "estimated_minutes": r["estimated_minutes"],
        "pinned": bool(r["pinned"]), "source": r["source"],
        "external_id": r["external_id"],
    }
    if extra:
        d.update(extra)
    return d


def course_dict(r):
    weights = None
    if r["weight_config_json"]:
        try:
            weights = json.loads(r["weight_config_json"])
        except (TypeError, ValueError):
            weights = None
    return {"id": r["id"], "name": r["name"],
            "subject_type": r["subject_type"], "weights": weights}


def get_or_create_course(name, subject_type=None):
    """Look up a course by exact name (dedup across manual add + sync); create
    with an inferred subject_type if absent. Returns course id."""
    name = (name or "").strip() or "未分類"
    row = query_one("SELECT id FROM courses WHERE name = ?", (name,))
    if row:
        return row["id"]
    st = subject_type or infer_subject_type(name)
    return write(
        "INSERT INTO courses (name, subject_type, created_at) VALUES (?,?,?)",
        (name, st, now_utc_iso()))


# --------------------------------------------------------------------------
# Schema (all tables created at once — no later ALTER TABLE column adds)
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    subject_type       TEXT NOT NULL DEFAULT 'memo',   -- stem|memo|lang|other
    weight_config_json TEXT,                            -- category weights (DB is source of truth)
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id               INTEGER PRIMARY KEY,
    course_id        INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    description      TEXT,
    due_at           TEXT,                              -- ISO8601 UTC
    category         TEXT NOT NULL DEFAULT 'homework',  -- homework|quiz|exam|project|other
    max_points       REAL,
    earned_points    REAL,
    status           TEXT NOT NULL DEFAULT 'todo',      -- proposed|todo|in_progress|submitted|graded
    estimated_minutes INTEGER DEFAULT 60,               -- 15|60|180
    pinned           INTEGER NOT NULL DEFAULT 0,
    source           TEXT NOT NULL DEFAULT 'manual',    -- manual|classroom|photo|pdf
    external_id      TEXT,                              -- MUST be NULL when absent (never '')
    updated_at       TEXT,
    last_synced_at   TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(source, external_id)                         -- prevents sync duplication
);
CREATE INDEX IF NOT EXISTS idx_assign_due    ON assignments(due_at);
CREATE INDEX IF NOT EXISTS idx_assign_status ON assignments(status);
CREATE INDEX IF NOT EXISTS idx_assign_course ON assignments(course_id);

CREATE TABLE IF NOT EXISTS materials (
    id             INTEGER PRIMARY KEY,
    course_id      INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    assignment_id  INTEGER REFERENCES assignments(id) ON DELETE SET NULL,
    kind           TEXT NOT NULL,                       -- photo|pdf|classroom_material
    original_path  TEXT,
    status         TEXT NOT NULL DEFAULT 'extracting',  -- extracting|extracted|generating|done|failed
    error_message  TEXT,
    extracted_text TEXT,
    extracted_json TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,          -- total claude tries (poison-pill guard)
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mat_status ON materials(status);

CREATE TABLE IF NOT EXISTS cards (
    id               INTEGER PRIMARY KEY,
    course_id        INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    material_id      INTEGER REFERENCES materials(id) ON DELETE SET NULL,
    card_type        TEXT,                              -- qa|term|cloze|steps
    front            TEXT NOT NULL,
    back             TEXT NOT NULL,
    topic            TEXT,
    origin           TEXT NOT NULL DEFAULT 'generated', -- extracted|generated
    source_quote     TEXT,
    confidence       TEXT,                              -- high|low (generated) | NULL
    verified         TEXT,                              -- NULL|ok|wrong
    content_hash     TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'new',       -- proposed|new|learning|review|suspended
    next_due_at      TEXT,                              -- ISO8601 UTC (NULL for new)
    repetitions      INTEGER NOT NULL DEFAULT 0,        -- SM-2 consecutive correct count
    current_interval INTEGER NOT NULL DEFAULT 0,        -- days
    current_ease     REAL NOT NULL DEFAULT 2.5,
    created_at       TEXT NOT NULL,
    UNIQUE(course_id, content_hash)                     -- prevents regen duplication
);
CREATE INDEX IF NOT EXISTS idx_cards_due    ON cards(next_due_at);
CREATE INDEX IF NOT EXISTS idx_cards_state  ON cards(state);
CREATE INDEX IF NOT EXISTS idx_cards_course ON cards(course_id);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY,
    card_id      INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    reviewed_at  TEXT NOT NULL,                         -- ISO8601 UTC
    grade        TEXT NOT NULL,                         -- again|hard|good|easy
    interval_days INTEGER,
    ease_factor  REAL
);
CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews(card_id);

CREATE TABLE IF NOT EXISTS study_guides (
    id         INTEGER PRIMARY KEY,
    course_id  INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    scope_desc TEXT,
    content_md TEXT,
    created_at TEXT NOT NULL
);
"""


def _ensure_column(conn, table, column, decl):
    """Idempotent additive migration: add `column` to `table` if missing.

    Guarded via PRAGMA table_info — no migration engine, just a safe ALTER for
    columns introduced after the initial schema (e.g. materials.attempts)."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# --------------------------------------------------------------------------
# Migration ledger — additive, idempotent, NON-transactional (plan S1)
# --------------------------------------------------------------------------
# Python 3.9's sqlite3 auto-commits before DDL and executescript() runs its own
# COMMITs, so a whole run can NOT be one transaction. Instead each step is written
# idempotently (IF NOT EXISTS / _ensure_column) and recorded in schema_migrations
# only AFTER it applies — a crash mid-run re-runs cleanly from the first unrecorded
# step (already-applied idempotent DDL is a harmless no-op). Before a phase's first
# unapplied step — and only when the DB already held user data — a WAL-safe .bak is
# taken so a bad migration can never lose it (guardrail 4 / plan S3).
_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    phase      TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

# Phase B1: notes/outliner storage. docs -- rems (a fractional-indexed outline).
# All-additive: no existing table is touched. daily_date is a LOCAL calendar-day
# label ('YYYY-MM-DD'), NOT a D-4 UTC stamp (it names "today's note" for a human).
_B1_DOCS_REMS = """
CREATE TABLE IF NOT EXISTS docs (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    course_id  INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    is_daily   INTEGER NOT NULL DEFAULT 0,
    daily_date TEXT,
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_daily ON docs(daily_date) WHERE is_daily=1;

CREATE TABLE IF NOT EXISTS rems (
    id         INTEGER PRIMARY KEY,
    doc_id     INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES rems(id) ON DELETE CASCADE,
    position   REAL NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    rem_type   TEXT NOT NULL DEFAULT 'bullet',
    props_json TEXT,
    collapsed  INTEGER NOT NULL DEFAULT 0,
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rems_doc ON rems(doc_id, parent_id, position);
"""


def _migrate_b1(conn):
    conn.executescript(_B1_DOCS_REMS)


def _migrate_e_source_loc(conn):
    # Phase E: a card's exact spot in its source file — a nullable JSON blob, e.g.
    # {"quote","char_start","char_end"} for text or {"region":[x,y,w,h]} for an
    # image. Deliberately generic (bare TEXT) so any later E4 capture model fits
    # without a further migration.
    _ensure_column(conn, "cards", "source_loc", "TEXT")


_G2_METHODS_DDL = """
CREATE TABLE IF NOT EXISTS study_methods (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    base        TEXT NOT NULL DEFAULT 'qa',  -- built-in this preset derives from
    instruction TEXT,                        -- free-text override for the AI recast
    created_at  TEXT NOT NULL
);
"""


def _migrate_g2_methods(conn):
    # Phase G2: user-saved custom study methods (preset + free-text instruction).
    # Built-in card-format methods live in code (methods.py); this table holds
    # only the custom presets a user creates.
    conn.executescript(_G2_METHODS_DDL)


def _migrate_h0_media(conn):
    # Phase H0: per-modality render structure (ordered steps, list items, cloze
    # info, self-grading rubric, MC choices, occlusion regions) as a nullable JSON
    # blob. Deliberately EXCLUDED from the D-5 content_hash — front/back stay the
    # card's identity — so richer modalities never disturb dedup/regenerate.
    _ensure_column(conn, "cards", "media_json", "TEXT")


# Phase I: material study modes (quiz / summary) + note folders. All additive.
_I_QUIZZES_DDL = """
CREATE TABLE IF NOT EXISTS quizzes (
    id             INTEGER PRIMARY KEY,
    material_id    INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    course_id      INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    format         TEXT NOT NULL DEFAULT 'written',   -- written|mixed
    scope_desc     TEXT,                               -- optional range note
    questions_json TEXT NOT NULL,                      -- [{type,question,answer,choices?}]
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quizzes_material ON quizzes(material_id);
"""


def _migrate_i_quizzes(conn):
    # Phase I: material-level comprehension quizzes (a one-shot test to grasp a
    # material's whole range — deliberately SEPARATE from the SRS review queue).
    # One row per generated quiz; questions_json holds the entire question set.
    conn.executescript(_I_QUIZZES_DDL)


def _migrate_i_summary_material(conn):
    # Phase I: let a study_guide (要点まとめ) attach to a specific material, not only
    # a course — the "まとめ" study mode summarizes one uploaded material.
    _ensure_column(conn, "study_guides", "material_id", "INTEGER")


_I_FOLDERS_DDL = """
CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    position   REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def _migrate_i_folders(conn):
    # Phase I: user-named folders to organize notes (docs) freely. docs.folder_id
    # is nullable (NULL = 未分類). A folder delete nulls its docs' folder_id in code
    # (no FK-cascade reliance, since the foreign_keys pragma may be off).
    conn.executescript(_I_FOLDERS_DDL)
    _ensure_column(conn, "docs", "folder_id", "INTEGER")


def _migrate_j_translation(conn):
    # Phase J: cached JA/EN translations of a card's front/back so a learner can flip
    # a card's language while reviewing. A nullable JSON blob keyed by lang code
    # ({"ja": {"front","back"}, "en": {"front","back"}}), EXCLUDED from the D-5
    # content_hash — front/back stay the card's identity, so translating never
    # dedups, reflows, or resets SRS state.
    _ensure_column(conn, "cards", "translation_json", "TEXT")


def _migrate_k_glossary(conn):
    # Phase K: one canonical term table per material, so every AI call about that
    # material spells a term the same way. TERM_RULE only binds WITHIN a single
    # output — each call is independent, so a summary could say 鋳型 while a card
    # said テンプレート for the same concept. A nullable JSON blob
    # ({"terms": [{"src": "...", "ja": "...", "en": "..."}]}) built once, lazily,
    # and injected into later prompts. Purely additive: it feeds prompts only and
    # never touches card front/back, so the D-5 content_hash is unaffected.
    _ensure_column(conn, "materials", "glossary_json", "TEXT")


MIGRATIONS = [
    ("b1_docs_rems", "B", _migrate_b1),
    ("e_source_loc", "E", _migrate_e_source_loc),
    ("g2_study_methods", "G", _migrate_g2_methods),
    ("h0_media", "H", _migrate_h0_media),
    ("i_quizzes", "I", _migrate_i_quizzes),
    ("i_summary_material", "I", _migrate_i_summary_material),
    ("i_folders", "I", _migrate_i_folders),
    ("j_card_translation", "J", _migrate_j_translation),
    ("k_material_glossary", "K", _migrate_k_glossary),
]


def apply_migrations(steps, backup_existing=False):
    """Apply each (version, phase, fn) exactly once, recording it after success."""
    conn = get_conn()
    with _write_lock:
        conn.executescript(_LEDGER_DDL)
        conn.commit()
    applied = {r["version"] for r in
               conn.execute("SELECT version FROM schema_migrations")}
    backed_up = set()
    for version, phase, fn in steps:
        if version in applied:
            continue
        if backup_existing and phase not in backed_up:
            try:
                import backup
                backup.pre_migration_backup(phase)
            except Exception as e:  # a backup hiccup must not block the migration
                print(f"[migrate] pre-migration backup ({phase}) failed: {e}")
            backed_up.add(phase)
        with _write_lock:
            fn(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, phase, applied_at) "
                "VALUES (?,?,?)", (version, phase, now_utc_iso()))
            conn.commit()


def init():
    # Whether the DB already held user data BEFORE we touch it: a phase's first
    # migration backs up existing data, but a brand-new / test DB does not (nothing
    # to protect, and tests stay side-effect-free). Checked before get_conn(),
    # which would otherwise create the file.
    preexisting = os.path.exists(DB_PATH)
    conn = get_conn()
    with _write_lock:
        conn.executescript(SCHEMA)
        # additive migrations for DBs created before a column existed
        _ensure_column(conn, "materials", "attempts",
                       "INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    apply_migrations(MIGRATIONS, backup_existing=preexisting)


# --------------------------------------------------------------------------
# Materials: total-attempt counter (poison-pill / runaway-cost guard)
# --------------------------------------------------------------------------
def max_material_attempts():
    return int(load_settings().get("max_material_attempts", 5))


def material_attempts(mid):
    r = query_one("SELECT attempts FROM materials WHERE id=?", (mid,))
    return (r["attempts"] if r else 0) or 0


def bump_material_attempts(mid):
    """Increment + commit BEFORE a claude call, so even a hard crash mid-call is
    counted (otherwise a crash-reinject loop bills claude forever)."""
    write("UPDATE materials SET attempts = COALESCE(attempts, 0) + 1 WHERE id=?",
          (mid,))


if __name__ == "__main__":
    init()
    tables = [r["name"] for r in query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("tables:", tables)
