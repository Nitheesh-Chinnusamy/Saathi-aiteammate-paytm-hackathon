"""notify.py - sends merchant messages from language templates (merchant's language + English copy).

Production: Paytm app push / WhatsApp Business / SMS gateway with template approval.
"""
import json

from saathi.core import logger
from saathi.sim import seed
from saathi.core import store
from saathi.core.clock import clock


_cache = {}


def localise(text_en, lang):
    """English → the merchant's language. Live: Gemini (cached). Mock: stays English."""
    if lang == "en" or not text_en:
        return text_en
    from saathi.integrations import llm
    if not llm.is_live():
        return text_en
    key = (lang, text_en)
    if key not in _cache:
        _cache[key] = llm.translate_to(text_en, seed.LANGUAGES.get(lang, {}).get("name", lang)) or text_en
    return _cache[key]


def send(merchant_id, case_id, kind, lang, choices=None, **params):
    text_en = seed.template("en", kind, **params)
    text = seed.template(lang, kind, **params) if seed.has_template(lang, kind) else localise(text_en, lang)
    store.insert("messages", {"merchant_id": merchant_id, "case_id": case_id, "direction": "out", "kind": kind,
                              "text": text, "text_en": text_en, "lang": lang, "ts": clock.now_iso(),
                              "choices": json.dumps(choices, ensure_ascii=False) if choices else None})
    logger.log("notify", f"→ {merchant_id} [{lang}] {kind}: \"{text[:70]}{'…' if len(text) > 70 else ''}\"")
    return {"kind": kind, "text": text, "text_en": text_en, "lang": lang, "choices": choices}


def send_text(merchant_id, case_id, kind, lang, text, text_en=None, choices=None):
    """A free-text reply (written by Gemini in live mode, or built from records)."""
    store.insert("messages", {"merchant_id": merchant_id, "case_id": case_id, "direction": "out", "kind": kind,
                              "text": text, "text_en": text_en, "lang": lang, "ts": clock.now_iso(),
                              "choices": json.dumps(choices, ensure_ascii=False) if choices else None})
    logger.log("notify", f"→ {merchant_id} [{lang}] {kind}: \"{text[:70]}{'…' if len(text) > 70 else ''}\"")
    return {"kind": kind, "text": text, "text_en": text_en, "lang": lang, "choices": choices}


def record_inbound(merchant_id, case_id, text, text_en, lang):
    return store.insert("messages", {"merchant_id": merchant_id, "case_id": case_id, "direction": "in", "kind": "merchant",
                                     "text": text, "text_en": text_en, "lang": lang, "ts": clock.now_iso()})


def link_to_case(message_id, case_id):
    store.ex("UPDATE messages SET case_id=? WHERE id=?", (case_id, message_id))


def thread(merchant_id):
    rows = store.q("SELECT * FROM messages WHERE merchant_id=? ORDER BY id", (merchant_id,))
    for r in rows:
        r["choices"] = store.jload(r["choices"])
    return rows
