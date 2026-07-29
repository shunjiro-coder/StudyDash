"""Phase G2: study-method catalog.

A "method" decides HOW a piece of content becomes a review card. Built-in
card-format methods live here in code; custom presets (a name + a free-text
instruction that steers the AI) live in the `study_methods` table. Picking a
method on a card triggers an AI recast (see generate.recast_card).
"""

import db

BUILTINS = [
    {"id": "qa", "name": "一問一答", "desc": "問いと短い答え", "card_type": "qa",
     "instruction": "問い(front)と短い答え(back)の一問一答にする。"},
    {"id": "term", "name": "用語カード", "desc": "用語→定義", "card_type": "term",
     "instruction": "用語(front)とその定義(back)にする。"},
    {"id": "cloze", "name": "穴埋め", "desc": "重要語を隠す", "card_type": "cloze",
     "instruction": "重要語を空欄「___」にした一文(front)と、空欄に入る語(back)にする。"},
    {"id": "steps", "name": "解法ステップ", "desc": "手順で覚える", "card_type": "steps",
     "instruction": "問い(front)と、解法の手順・着眼点(back)。最終的な数値の答えは書かない。"},
    {"id": "elaborate", "name": "自己説明", "desc": "自分の言葉で説明",
     "card_type": "elaborate",
     "instruction": "「〜を自分の言葉で説明してみよう」という問い(front)と、模範となる説明(back)にする。"},
]
_BUILTIN_BY_ID = {m["id"]: m for m in BUILTINS}


def custom_dict(r):
    base = _BUILTIN_BY_ID.get(r["base"], BUILTINS[0])
    return {
        "id": "custom:%d" % r["id"], "name": r["name"], "desc": "独自メソッド",
        "card_type": base["card_type"], "instruction": r["instruction"] or "",
        "base": r["base"], "builtin": False, "custom_id": r["id"],
    }


def all_methods():
    out = [dict(m, builtin=True) for m in BUILTINS]
    out += [custom_dict(r) for r in
            db.query("SELECT * FROM study_methods ORDER BY id DESC")]
    return out


def get_method(mid):
    """Resolve a method id ('qa' or 'custom:3') to its dict, or None."""
    if not mid:
        return None
    if mid.startswith("custom:"):
        try:
            cid = int(mid.split(":", 1)[1])
        except ValueError:
            return None
        r = db.query_one("SELECT * FROM study_methods WHERE id=?", (cid,))
        return custom_dict(r) if r else None
    m = _BUILTIN_BY_ID.get(mid)
    return dict(m, builtin=True) if m else None
