"""Phase E5: source-location (完全解析) — locate a verbatim quote inside the
transcription so the viewer can highlight WHERE a card came from.

Covers db.locate() matching, the generate/ingest pipelines attaching source_loc,
the regenerate-backfill UPSERT (location refreshed, SRS state preserved), and the
review queue exposing material_id + source_loc so the study screen can jump back.
"""

import json

import db
import generate
import ingest
import srs
from tests import seed


# ---- db.locate ------------------------------------------------------------
def test_locate_exact_offsets_map_to_original():
    t = "abc 光合成の定義 xyz"
    loc = db.locate("光合成の定義", t)
    assert t[loc["char_start"]:loc["char_end"]] == "光合成の定義"


def test_locate_is_nfkc_width_insensitive():
    t = "ページ３の問１を参照"          # full-width digits in the source
    loc = db.locate("問1", t)            # half-width digit in the quote
    assert loc["char_start"] is not None
    assert t[loc["char_start"]:loc["char_end"]] == "問１"


def test_locate_tolerates_whitespace_runs():
    t = "光合成   は   光"
    loc = db.locate("光合成 は 光", t)
    assert loc["char_start"] == 0
    assert loc["char_end"] == len(t)


def test_locate_not_found_keeps_quote_null_offsets():
    loc = db.locate("存在しない語", "まったく別の文章")
    assert loc["quote"] == "存在しない語"
    assert loc["char_start"] is None and loc["char_end"] is None


def test_locate_empty_quote():
    loc = db.locate("", "本文")
    assert loc["char_start"] is None


# ---- generate pipeline attaches + backfills location ----------------------
def test_generate_attaches_and_backfills_location(monkeypatch):
    co = seed.make_course(subject_type="memo")
    text = "光合成は光を使い水と二酸化炭素から有機物を合成する。"
    mid = seed.make_material(co, extracted_text=text)
    obj = {"study_guide_md": "", "cards": [
        {"front": "光合成とは？", "back": "光で有機物を作る反応",
         "source_quote": "有機物を合成する", "card_type": "qa"}]}
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: obj)

    added, _ = generate.generate_for_material(mid)
    assert added == 1
    card = db.query_one("SELECT * FROM cards WHERE material_id=?", (mid,))
    loc = json.loads(card["source_loc"])
    assert loc["quote"] == "有機物を合成する"
    assert text[loc["char_start"]:loc["char_end"]] == "有機物を合成する"

    # Simulate real SRS history, then regenerate with a DIFFERENT quote: only the
    # location refreshes; front/back and SRS state are preserved (D-5 dedup path).
    db.write("UPDATE cards SET state='review', repetitions=4, current_ease=2.3 "
             "WHERE id=?", (card["id"],))
    obj["cards"][0]["source_quote"] = "光を使い水"
    generate.generate_for_material(mid)

    c2 = db.query_one("SELECT * FROM cards WHERE id=?", (card["id"],))
    assert c2["repetitions"] == 4 and c2["state"] == "review"   # SRS untouched
    assert c2["current_ease"] == 2.3
    assert json.loads(c2["source_loc"])["quote"] == "光を使い水"  # location refreshed
    # no duplicate card was created
    n = db.query_one("SELECT COUNT(*) n FROM cards WHERE material_id=?", (mid,))["n"]
    assert n == 1


def test_generated_card_without_quote_has_null_source_loc(monkeypatch):
    co = seed.make_course(subject_type="memo")
    mid = seed.make_material(co, extracted_text="本文")
    obj = {"study_guide_md": "", "cards": [{"front": "Q", "back": "A"}]}
    monkeypatch.setattr(generate, "_generate_with_retry", lambda *a, **k: obj)
    generate.generate_for_material(mid)
    card = db.query_one("SELECT * FROM cards WHERE material_id=?", (mid,))
    assert card["source_loc"] is None


# ---- ingest pipeline (already-existing Q&A) -------------------------------
def test_extracted_cards_get_location():
    co = seed.make_course()
    text = "ABC 定義：光合成 DEF"
    mid = seed.make_material(co, extracted_text=text)
    n = ingest._insert_extracted_cards(
        co, mid, [{"front": "光合成", "back": "…", "source_quote": "定義：光合成"}],
        text)
    assert n == 1
    card = db.query_one("SELECT * FROM cards WHERE material_id=?", (mid,))
    loc = json.loads(card["source_loc"])
    assert text[loc["char_start"]:loc["char_end"]] == "定義：光合成"


# ---- review queue exposes the jump-back fields ----------------------------
def test_review_queue_exposes_material_and_source_loc():
    co = seed.make_course()
    mid = seed.make_material(co)
    cid = seed.make_card(co, "Q", "A", state="new", material_id=mid)
    db.write("UPDATE cards SET source_loc=? WHERE id=?",
             (json.dumps({"quote": "根拠", "char_start": 0, "char_end": 2}), cid))
    q = srs.get_queue()
    card = next(c for c in q["cards"] if c["id"] == cid)
    assert card["material_id"] == mid
    assert card["source_loc"]["quote"] == "根拠"
