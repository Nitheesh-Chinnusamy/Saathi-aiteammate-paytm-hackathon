"""conversation.py - the chat layer in front of the 7 steps.

Every message is first classified: greeting / thanks / status question / complaint / other.
Only a complaint starts Step 1-7. Everything else gets a short reply in the merchant's language:
  live  -> Gemini writes the reply (and classifies) from the merchant's real request status
  mock  -> keyword rules + reply templates
A seeded scenario sentence is always a complaint (no LLM needed).

Production: a small intent classifier + the same dialogue policy; Gemini/open model for replies.
"""
import re

from saathi.integrations import llm
from saathi.core import logger
from saathi.services import notify
from saathi.core import store
from saathi.core.clock import parse
from saathi.sim.seed import LANGUAGES, SCENARIOS

GREET = ["hello", "hi", "hey", "hii", "namaste", "namaskar", "namaskaram", "vanakkam", "good morning", "good evening",
         "sat sri akal", "హలో", "నమస్కారం", "నమస్తే", "नमस्ते", "नमस्कार", "हैलो", "வணக்கம்", "ನಮಸ್ಕಾರ", "നമസ്കാരം",
         "নমস্কার", "નમસ્તે", "ਸਤ ਸ੍ਰੀ ਅਕਾਲ", "ନମସ୍କାର"]
THANKS = ["thank", "thanks", "dhanyavad", "shukriya", "धन्यवाद", "शुक्रिया", "ధన్యవాదాలు", "நன்றி", "ಧನ್ಯವಾದ", "നന്ദി",
          "ধন্যবাদ", "આભાર", "ਧੰਨਵਾਦ", "ଧନ୍ୟବାଦ"]
STATUS = ["status", "update", "what happened", "my request", "my complaint", "my ticket", "स्टेटस", "स्थिति", "స్టేటస్", "நிலை", "ಸ್ಥಿತಿ", "സ്റ്റാറ്റസ്", "स्थिती", "অবস্থা", "સ્ટેટસ", "ਸਟੇਟਸ", "ସ୍ଥିତି", "kya hua"]
PROBLEM = ["rent", "refund", "money", "paisa", "payment", "settlement", "soundbox", "sound box", "alert", "charged",
           "deducted", "cut", "credit", "account", "kiraya", "not come", "not working", "किराया", "पैसा", "पैसे", "रिफंड",
           "అద్దె", "రెంట్", "డబ్బు", "வாடகை", "பணம்", "ಬಾಡಿಗೆ", "ಹಣ", "വാടക", "പണം", "भाडे", "টাকা", "ভাড়া", "પૈસા",
           "ભાડું", "ਪੈਸੇ", "ਕਿਰਾਇਆ", "ଟଙ୍କା", "ଭଡ଼ା", "qr"]
INTENTS = ("greeting", "thanks", "status", "complaint", "other")


def _has(words, text):
    t = text.lower()
    return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", t) if w.isascii() else w in t for w in words)


def seeded_english(text):
    t = (text or "").strip()
    sc = next((s for s in SCENARIOS.values() if t in {v.strip() for v in s["texts"].values()}), None)
    return sc["text_en"] if sc else None


def classify_rules(original, english):
    both = f"{original} {english or ''}"
    if _has(STATUS, both):
        return "status"
    problem = _has(PROBLEM, both)
    if not problem and _has(THANKS, both):
        return "thanks"
    if not problem and _has(GREET, both):
        return "greeting"
    return "complaint" if problem else "other"


# ------------------------------------------------------------------ request status, in plain words
def latest_case(merchant_id):
    r = store.q1("SELECT id FROM cases WHERE merchant_id=? AND source='chat' ORDER BY created_at DESC, id DESC LIMIT 1", (merchant_id,))
    return store.load_case(r["id"]) if r else None


def status_facts(merchant_id):
    """(template kind, params, English fact sentence for Gemini)"""
    c = latest_case(merchant_id)
    if not c:
        return "status_none", {}, "The merchant has no requests yet."
    st = c.get("status")
    if st == "CLOSED":
        return "status_done", {}, "Their last request is fixed and confirmed in Paytm's records."
    if st in ("DENIED", "REJECTED"):
        return "status_blocked", {}, "Their last request was refused for safety (money cannot go to an unlinked account)."
    if st in ("AWAITING_APPROVAL", "NEEDS_HUMAN", "ESCALATED", "REMEDIATING"):
        return "status_team", {}, "A Paytm team member is working on their request right now."
    k = store.q1("SELECT deadline FROM contracts WHERE case_id=?", (c["id"],))
    if k:
        eta = parse(k["deadline"]).strftime("%d %b %H:%M")
        return "status_progress", {"eta": eta}, f"The fix is done and Saathi is waiting to confirm it in Paytm's records by {eta}."
    return "status_progress", {"eta": "soon"}, "Their request is being worked on."


# ------------------------------------------------------------------ main entry
def respond(merchant_id, name, lang, original, english, focus=False):
    """Returns {'intent', 'reply'} - reply is None for complaints (the 7 steps take over).
    focus=True (demo UI): anything that isn't a greeting / thanks / status question is treated as a request."""
    seeded = seeded_english(original)
    if seeded:
        logger.log("match", "message is a known problem description → complaint · start the 7 steps")
        return {"intent": "complaint", "english": seeded}
    from saathi.steps import step1_understand as s1
    if english and (s1.EXPLAIN.search(english) or s1.PAYMENT_CHECK.search(english)) or s1.EXPLAIN.search(original or ""):
        logger.log("match", "message asks about payments/deductions in the records → complaint · start the 7 steps")
        return {"intent": "complaint", "english": english}
    kind, params, facts = status_facts(merchant_id)
    if llm.is_live():
        out = llm.converse(original, LANGUAGES[lang]["name"], name, facts)
        if out:
            if out["intent"] == "complaint" or (focus and out["intent"] == "other"):
                return {"intent": "complaint", "english": out.get("english")}
            msg = notify.send_text(merchant_id, None, f"chat_{out['intent']}", lang, out["reply"], out.get("reply_en"))
            return {"intent": out["intent"], "reply": msg}
    intent = classify_rules(original, english)
    if focus and intent == "other":
        intent = "complaint"
    logger.log("match", f"intent={intent} (keyword rules{', Gemini mock' if not llm.is_live() else ', Gemini unavailable'})"
                        + (" → start the 7 steps" if intent == "complaint" else " → reply only, no request opened"))
    if intent == "complaint":
        return {"intent": "complaint", "english": english}
    tkind = {"greeting": "greet", "thanks": "thanks", "status": kind, "other": "ask_problem"}[intent]
    msg = notify.send(merchant_id, None, tkind, lang, name=name, **params)
    return {"intent": intent, "reply": msg}
