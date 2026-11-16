"""step1_understand.py - Step 1 Understand: who is this (from login, never from their words) and
what is the problem.

  a) identity from the logged-in session; identity claims in the text are logged and ignored
  b) masked state card via context_service -> mcp_client
  c) hear: Sarvam (voice) or typed text (script detection, seeded English copy / Gemini translate)
  d) parse amounts (digits + Hindi/Telugu/English number words) and relative dates; snap ±1% (digits) / ±15% (number words)
  e) match against the merchant's OWN open anomalies (1 accept, 2+ TF-IDF with margin, 0 open-world)
  f) broken-promise detector (same-topic ticket closed without a fix -> priority HIGH)
  g) incident attach by data-built key, TF-IDF fallback, ONLY if the incident predicate passes
  h) validated case object

Production: same flow; identity from Paytm's auth session, ASR from Sarvam, embeddings for match.
"""
import re
from datetime import timedelta

from saathi.sim import anomaly_rules
from saathi.services import clustering
from saathi.services import context_service
from saathi.integrations import llm
from saathi.core import logger
from saathi.integrations import sarvam
from saathi.core import store
from saathi.core import trace
from saathi.core.clock import clock, iso, parse
from saathi.mcp.mcp_client import MCPClient
from saathi.sim.seed import CUSTOMERS, HEROES, LANGUAGES, SCENARIOS

DIRECTORY = {h[0]: {"id": h[0], "name": h[1], "business": h[2], "city": h[3], "lang": h[4], "type": "merchant"} for h in HEROES}
DIRECTORY.update({c[0]: {"id": c[0], "name": c[1], "business": "Paytm customer", "city": c[2], "lang": c[3], "type": "customer"}
                  for c in CUSTOMERS})
EXPLAIN = re.compile(r"\b(deductions?|explain|samjhao|katauti|charges today)\b|कटौती|కోతలు", re.I)
PAYMENT_CHECK = re.compile(r"\b(paid|pay kiya|payment)\b.*\b(no alert|not (get|got|getting|received)|got no|alert nahi|nahi aaya|didn'?t get)\b", re.I)

from saathi.sim import languages_extra  # noqa: E402

IDENTITY_CLAIMS = [r"\bowner\b", r"मालिक", r"యజమాని", r"\bi am the\b", r"मैं .* हूँ", r"\bmalik\b"] + languages_extra.OWNER_WORDS

NUMBER_WORDS = {
    "पाँच हज़ार": 5000, "पांच हजार": 5000, "पाँच हजार": 5000, "पांच हज़ार": 5000, "ఐదు వేలు": 5000, "five thousand": 5000,
    "हज़ार": 1000, "हजार": 1000, "వెయ్యి": 1000, "thousand": 1000,
    "छह सौ": 600, "छः सौ": 600, "ఆరు వందలు": 600, "six hundred": 600, "chhe sau": 600,
    "पाँच सौ": 500, "पांच सौ": 500, "ఐదు వందలు": 500, "five hundred": 500, "paanch sau": 500,
    "सौ": 100, "వంద": 100, "hundred": 100,
}
DATE_WORDS = {"कल": -1, "kal": -1, "నిన్న": -1, "yesterday": -1, "आज": 0, "aaj": 0, "ఈరోజు": 0, "today": 0,
              "परसों": -2, "parson": -2, "మొన్న": -2}
VPA = re.compile(r"[\w.\-]+@[a-z]{2,}", re.I)
SNAP_TOL = 0.01          # digits: the merchant read the number off a screen, so only ±1%
SNAP_TOL_WORDS = 0.15    # amount spoken in words ("paanch hazaar") is approximate: ±15%
CHOICE_MARGIN = 0.15
INC_SIM, INC_MARGIN = 0.35, 0.10


class CaseValidationError(Exception):
    pass


# ------------------------------------------------------------------ helpers
SCRIPTS = [("te", r"[ఀ-౿]"), ("hi", r"[ऀ-ॿ]"), ("ta", r"[஀-௿]"), ("kn", r"[ಀ-೿]"),
           ("ml", r"[ഀ-ൿ]"), ("bn", r"[ঀ-৿]"), ("gu", r"[઀-૿]"), ("pa", r"[਀-੿]"),
           ("od", r"[଀-୿]")]


def detect_script(text):
    for code, pattern in SCRIPTS:
        if re.search(pattern, text or ""):
            return code
    if re.search(r"\b(kar|nahi|hai|mujhe|raha|rahe|kya|mera|paisa|se|par)\b", (text or "").lower()):
        return "hinglish"
    return "en"


def parse_amounts(text):
    found, rest = [], VPA.sub(" ", text or "")
    for m in re.finditer(r"(?:₹|rs\.?|inr)\s*([\d,]+)", rest, re.I):
        found.append(int(m.group(1).replace(",", "")))
    rest2 = re.sub(r"(?:₹|rs\.?|inr)\s*[\d,]+", " ", rest, flags=re.I)
    for m in re.finditer(r"(?<![\w-])(\d{2,6})(?![\w-])", rest2):
        found.append(int(m.group(1)))
    low = rest2.lower()
    for phrase in sorted(NUMBER_WORDS, key=len, reverse=True):
        if phrase in low:
            found.append(NUMBER_WORDS[phrase])
            low = low.replace(phrase, " ")
    out = []
    for v in found:
        if v not in out:
            out.append(v)
    return out


def ledger_values(card):
    vals = [(c["amount"], c["id"]) for c in card.get("charges_30d", [])]
    vals += [(s["amount"], s["id"]) for s in card.get("settlements_7d", [])]
    vals += [(p["amount"], p["id"]) for p in card.get("payouts", [])]
    vals += [(t["amount"], t["id"]) for t in card.get("upi_txns", [])]
    return vals


def digit_amounts(text):
    """Amounts written with digits in the merchant's own words (as opposed to number words)."""
    rest = VPA.sub(" ", text or "")
    return {int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", rest) if x.replace(",", "").isdigit()}


def snap(amount, card, words=False):
    best = None
    tol = SNAP_TOL_WORDS if words else SNAP_TOL
    for v, rid in ledger_values(card):
        if abs(amount - v) <= tol * v and (best is None or abs(amount - v) < abs(amount - best[0])):
            best = (v, rid)
    return best


def parse_dates(text):
    low = (text or "").lower()
    out = []
    for w, d in DATE_WORDS.items():
        if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low):
            out.append({"word": w, "date": iso(clock.now() + timedelta(days=d))[:10]})
    return out


def _item_label(a):
    if a["rule"] == "settlement_held":
        return f"₹{a['amount']:,} settlement of {parse(a['since']).strftime('%d %b')} ({a['settlement_id']})"
    if a["rule"] == "incentive_pending":
        return f"₹{a['amount']:,} incentive payout ({a['payout_id']})"
    if a["rule"] == "rent_after_return":
        return f"₹{a['amount']:,} rent on returned {a['device_id']} ({a['charge_id']})"
    return a["summary"]


def predicate_data(client, card, incident):
    if incident["predicate"] == "rent_after_return_unbilled":
        rows = []
        for d in card["devices"]:
            if d["state"] == "RETURN_RECEIVED":
                rows += client.call("get_billing_events", {"device_id": d["id"]})["rows"]
        return {"billing_events": rows}
    return {}


def check_incident_predicate(client, card, incident):
    data = predicate_data(client, card, incident)
    return anomaly_rules.run_predicate(incident["predicate"], card, data, incident)


def _attach(case, inc, method, ev):
    store.insert("incident_members", {"incident_id": inc["id"], "case_id": case["id"], "merchant_id": case["merchant_id"],
                                      "ts": clock.now_iso()})
    store.ex("UPDATE incidents SET members = members + 1 WHERE id=?", (inc["id"],))
    case["incident"] = {"id": inc["id"], "title": inc["title"], "method": method, "signature": inc["signature"],
                        "predicate": inc["predicate"], "predicate_evidence": ev, "precision_holdout": inc["precision_holdout"]}


REQUIRED = ["id", "merchant_id", "lang", "text_english", "match", "priority", "status"]


def validate(case):
    missing = [k for k in REQUIRED if not case.get(k)]
    if missing:
        raise CaseValidationError(f"case missing {missing}")
    if case["match"]["status"] == "matched" and not case.get("anomaly"):
        raise CaseValidationError("matched case without anomaly")
    if case["match"]["status"] == "unmatched" and not case.get("area"):
        raise CaseValidationError("unmatched case without area")
    return True


# ------------------------------------------------------------------ main
def understand(merchant_id, text=None, audio=None, audio_name=None, audio_mime=None, scenario_id=None,
               choice=None, case=None, lang=None, heard=None):
    """Returns the case dict. `case` is passed when the merchant answers a choice question.
    `lang` is the reply language the merchant picked at login (defaults to their profile language)."""
    if case is None:
        case = {"id": store.next_id("C-", 9917), "merchant_id": merchant_id, "created_at": clock.now_iso(),
                "status": "OPEN", "gemini_calls": 0, "llm_used": "none", "source": "chat", "scenario_id": scenario_id}
    cid = case["id"]
    client = MCPClient(merchant_id)

    # a) identity -------------------------------------------------------
    with trace.substep(cid, 1, "Identity from login") as t:
        prof = DIRECTORY.get(merchant_id)
        if not prof:
            raise CaseValidationError(f"no session for {merchant_id}")
        lang = case.get("lang") or (lang if lang in LANGUAGES else prof["lang"])
        logger.log("auth", f"session → {merchant_id} · {prof['name']} · {prof['business']} · {prof['city']} · lang={lang}"
                           + (" (chosen at login)" if lang != prof["lang"] else ""))
        raw = text or (heard or {}).get("text_original") or case.get("text_original") or ""
        claims = [p for p in IDENTITY_CLAIMS if re.search(p, raw, re.I)]
        if claims:
            logger.log("auth", f"text claims identity/ownership ({len(claims)} pattern(s)) → ignored · identity stays {merchant_id} (session)", level="warn")
        case["identity_claims_ignored"] = bool(claims)
        case["lang"] = lang
        t["result"] = f"{merchant_id} · {prof['name']} (session)" + (" · text identity claim ignored" if claims else "")

    # b) state card -----------------------------------------------------
    with trace.substep(cid, 1, "State card via MCP") as t:
        card = context_service.get_state_card(merchant_id, client)
        case["merchant"] = card["merchant"]
        case["state_card_as_of"] = card["as_of"]
        t["result"] = f"{len(card['devices'])} devices · {len(card['charges_30d'])} charges · anomalies {[a['rule'] for a in card['open_anomalies']]}"
        t["evidence"] = [a["id"] for a in card["open_anomalies"]]

    # c) hear -----------------------------------------------------------
    if "text_english" not in case:
        with trace.substep(cid, 1, "Hear", ai="ASR" if (audio or heard) else "none") as t:
            if audio or heard:
                res = heard or sarvam.transcribe(audio, audio_name, audio_mime, scenario_id, merchant_id, case["lang"])
                if heard:
                    logger.log("parse", f"voice note already transcribed in the chat step ({res['language_code']}) → \"{res['text_english']}\"")
                if not res:
                    raise CaseValidationError("could not transcribe audio")
                case.update(text_original=res["text_original"], text_english=res["text_english"],
                            detected_lang=res["language_code"], input_mode="voice", asr_live=res["live"])
                t["ai"] = "ASR (live)" if res["live"] else "ASR (mock)"
            else:
                script = detect_script(text)
                seeded = next((s for s in SCENARIOS.values() if (text or "").strip() in {v.strip() for v in s["texts"].values()}), None)
                if seeded:
                    eng, how = seeded["text_en"], "seeded English copy"
                elif script == "en":
                    eng, how = text, "already English"
                else:
                    tr = llm.translate(text, case)
                    eng, how = (tr, "Gemini translate") if tr else (text, "pass-through (Gemini mock)")
                    if tr:
                        t["ai"] = "Gemini translate"
                logger.log("parse", f"typed text · script={script} · {how} → \"{eng}\"")
                case.update(text_original=text, text_english=eng, detected_lang=script, input_mode="text")
            t["result"] = f"\"{case['text_english']}\""

    # d) parse ----------------------------------------------------------
    with trace.substep(cid, 1, "Parse amounts & dates") as t:
        both = f"{case['text_original']} || {case['text_english']}"
        amounts = parse_amounts(both)
        parsed = {"amounts": [], "dates": parse_dates(both), "destination": None}
        digits = digit_amounts(case["text_original"])
        for a in amounts:
            words = a not in digits
            s = snap(a, card, words)
            parsed["amounts"].append({"said": a, "snapped": s[0] if s else None, "ref": s[1] if s else None,
                                      "in_words": words})
        m = VPA.search(case["text_original"] or "")
        if m:
            parsed["destination"] = m.group(0)
        parts = []
        for a in parsed["amounts"]:
            tol = "±15%, said in words" if a["in_words"] else "±1%, digits"
            parts.append(f"₹{a['said']:,} → snapped ₹{a['snapped']:,} ({a['ref']}, {tol})" if a["ref"]
                         else f"₹{a['said']:,} · no ledger value within {tol} → unsnapped")
        if parsed["dates"]:
            parts.append("dates " + ", ".join(f"{d['word']}={d['date']}" for d in parsed["dates"]))
        if parsed["destination"]:
            parts.append(f"destination {parsed['destination']} (not from session)")
        logger.log("parse", " · ".join(parts) if parts else "no amounts / dates / destinations in text")
        payout_words = re.search(r"\b(send|refund|transfer)\b.*\b(account|vpa|upi)\b", case["text_english"], re.I)
        en_text = f"{case['text_english']} {case['text_original']}"
        is_merchant = prof.get("type") == "merchant"
        if choice:
            case["intent"] = "issue"
        elif parsed["destination"] or payout_words:
            case["intent"] = "payout_request"
        elif is_merchant and EXPLAIN.search(en_text):
            case["intent"] = "explain_today"
        elif is_merchant and parsed["amounts"] and PAYMENT_CHECK.search(case["text_english"]):
            case["intent"] = "payment_check"
        else:
            case["intent"] = "issue"
        if case["intent"] in ("explain_today", "payment_check"):
            logger.log("parse", f"intent={case['intent']}" + (f" · amount ₹{parsed['amounts'][0]['said']:,}" if parsed["amounts"] else ""))
        case["parsed"] = parsed
        t["result"] = "; ".join(parts) or "nothing to parse"
        t["evidence"] = [a["ref"] for a in parsed["amounts"] if a["ref"]]

    # e) match ----------------------------------------------------------
    with trace.substep(cid, 1, "Match to merchant's own items", ai="clustering") as t:
        cands = card["open_anomalies"]
        en = case["text_english"]
        ranked, margin = clustering.score_clusters(en)
        logger.log("cluster", f"tf-idf nearest {clustering.fmt(ranked, margin)}")
        case["cluster_scores"] = ranked[:3]
        anomaly, status = None, None
        if choice:
            anomaly = next((a for a in cands if a["id"] == choice), None)
            if not anomaly:
                raise CaseValidationError(f"choice {choice} is not one of the merchant's items")
            logger.log("match", f"merchant chose {choice} ({anomaly['rule']}) → accepted")
            status = "matched"
        elif case["intent"] in ("explain_today", "payment_check"):
            logger.log("match", f"candidates={len(cands)} · {case['intent']} is a question about records, not one open item → own path")
            status = "unmatched"
        elif case["intent"] == "payout_request":
            logger.log("match", f"candidates={len(cands)} · text is a payout request, not about an open item → open-world")
            status = "unmatched"
        elif len(cands) == 1:
            c = cands[0]
            aranked, amargin = clustering.score_areas(en)
            top_area, top_s = aranked[0]
            cand_area_s = dict(aranked).get(c["area"], 0.0)
            if top_area != c["area"] and top_s >= 0.3 and top_s - cand_area_s >= 0.2:
                logger.log("match", f"candidates=1 {c['rule']} but text is clearly about {top_area} ({top_s:.2f}) → open-world")
                status = "unmatched"
            else:
                anomaly, status = c, "matched"
                logger.log("match", f"candidates=1 {c['rule']} → accepted")
        elif len(cands) >= 2:
            rules = [c["rule"] for c in cands]
            cr, cm = clustering.score_clusters(en, rules)
            logger.log("match", f"candidates={len(cands)} {rules} · {clustering.fmt(cr, cm)}")
            if cm < CHOICE_MARGIN:
                status = "choice"
                case["choices"] = [{"anomaly_id": c["id"], "label": _item_label(c), "rule": c["rule"]} for c in cands]
                logger.log("match", f"margin {cm:.2f} < {CHOICE_MARGIN} → ask one-tap choice from merchant's own items")
            else:
                anomaly = next(c for c in cands if c["rule"] == cr[0][0])
                status = "matched"
                logger.log("match", f"margin {cm:.2f} ≥ {CHOICE_MARGIN} → {anomaly['rule']}")
        else:
            logger.log("match", "candidates=0 → unmatched (open-world path)")
            status = "unmatched"
        case["match"] = {"status": status, "candidates": [c["id"] for c in cands], "margin": margin}
        if anomaly:
            case["anomaly"] = anomaly
            case["issue_type"] = anomaly["rule"]
            case["area"] = anomaly["area"]
            t["evidence"] = anomaly["evidence_ids"]
        t["result"] = {"matched": f"{anomaly['rule']} ({anomaly['id']})" if anomaly else "",
                       "choice": f"ask: {len(cands)} items, margin {margin:.2f}", "unmatched": "open-world"}[status]
        if status == "choice":
            case["status"] = "AWAITING_CHOICE"
            case["priority"] = "NORMAL"
            validate(case)
            store.save_case(case)
            return case

    # f) broken promise ---------------------------------------------------
    with trace.substep(cid, 1, "Broken-promise check") as t:
        case["priority"], case["prior_fix_failed"] = "NORMAL", False
        topic = None
        if case.get("issue_type"):
            cc = store.q1("SELECT ticket_topic FROM cluster_cards WHERE issue_type=?", (case["issue_type"],))
            topic = cc and cc["ticket_topic"]
        prior = [tk for tk in card["tickets_90d"] if topic and tk["topic"] == topic and tk["status"] == "CLOSED"
                 and not tk["credit_issued"]]
        if prior:
            tk = prior[-1]
            case.update(priority="HIGH", prior_fix_failed=True, prior_ticket=tk["id"])
            logger.log("promise", f"{tk['id']} closed without credit → prior_fix_failed · priority=HIGH")
            t["result"], t["evidence"] = f"{tk['id']} closed without fix → HIGH", [tk["id"]]
        else:
            logger.log("promise", "no same-topic ticket closed without fix · priority=NORMAL")
            t["result"] = "no broken promise"

    # g) incident -------------------------------------------------------
    with trace.substep(cid, 1, "Incident attach") as t:
        active = store.q("SELECT * FROM incidents WHERE status='ACTIVE'")
        case["incident"] = None
        if case.get("anomaly"):
            key = anomaly_rules.incident_key(case["anomaly"])
            case["key"] = key
            inc = next((i for i in active if i["signature"] == key), None)
            if inc:
                ok, ev = check_incident_predicate(client, card, inc)
                logger.log("incident", f"key {key} → {inc['id']} · predicate {'✓' if ok else '✗'}")
                if ok:
                    _attach(case, inc, "key", ev)
            a = case["anomaly"]
            if not case["incident"] and a.get("bank_prefix"):
                # no exact key: is there an active incident at the SAME bank? predicate = the anomaly's own bank matches
                inc = next((i for i in active if i["signature"].split("|")[1:2] == [a["bank_prefix"]]), None)
                if inc:
                    logger.log("incident", f"no incident for key {key} · same bank {a['bank_prefix']} as active {inc['id']} → related · "
                                           f"predicate ✓ ({a['txn_id']} at {a['bank']})")
                    _attach(case, inc, "bank", [a["txn_id"], inc["id"]])
        if not case["incident"]:
            ranked_i, m_i = clustering.score_incidents(case["text_english"], active)
            if ranked_i and ranked_i[0][1] >= INC_SIM and m_i >= INC_MARGIN:
                inc = next(i for i in active if i["id"] == ranked_i[0][0])
                ok, ev = check_incident_predicate(client, card, inc)
                logger.log("incident", f"tf-idf fallback {inc['id']} sim {ranked_i[0][1]:.2f} margin {m_i:.2f} · predicate "
                                       f"{'✓ → attach' if ok else '✗ on this merchant data → NOT attached (false-match guard)'}")
                if ok:
                    _attach(case, inc, "tfidf", ev)
            elif not case.get("key"):
                logger.log("incident", f"no incident key · tf-idf best {ranked_i[0][0] if ranked_i else '-'} "
                                       f"{ranked_i[0][1] if ranked_i else 0:.2f} < {INC_SIM} → none")
            elif not case["incident"]:
                logger.log("incident", f"key {case['key']} → no active incident")
        inc = case["incident"]
        t["result"] = f"{inc['id']} ({inc['method']}, predicate ✓)" if inc else "no incident"
        t["evidence"] = (inc or {}).get("predicate_evidence", [])

    # h) case object ----------------------------------------------------
    with trace.substep(cid, 1, "Case object") as t:
        if case["match"]["status"] == "unmatched":
            aranked, am = clustering.score_areas(case["text_english"])
            case["area"] = aranked[0][0]
            case["area_runner_up"] = aranked[1][0]
            case["issue_type"] = (case["intent"] if case["intent"] in ("payout_request", "explain_today", "payment_check")
                                  else case.get("issue_type") or "novel")
            logger.log("cluster", f"area by tf-idf: {clustering.fmt(aranked, am)}")
        case["status"] = "UNDERSTOOD"
        validate(case)
        store.save_case(case)
        t["result"] = f"{case['id']} · {case['issue_type']} · area {case['area']} · priority {case['priority']} ✓ validated"
    case["_card"] = card
    return case
