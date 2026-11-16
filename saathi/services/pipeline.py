"""pipeline.py - orchestrates the 7 steps for a merchant message, choice answers, human-desk
decisions, the bank-outage spike, and wires the event subscribers (Step 6 verify).

Production: a durable workflow (Temporal) per case; here plain functions under one lock.
"""
import re
import threading
import time

from saathi.core import event_bus
from saathi.services import human_desk
from saathi.services import learning
from saathi.integrations import llm
from saathi.core import logger
from saathi.services import notify
from saathi.sim import paytm_sim
from saathi.sim import seed
from saathi.steps import step1_understand
from saathi.steps import step2_diagnose
from saathi.steps import step3_safety
from saathi.steps import step4_act
from saathi.steps import step5_contract
from saathi.steps import step6_verify
from saathi.core import store
from saathi.core import trace
from saathi.sim import anomaly_rules
from saathi.services import clustering
from saathi.services import conversation
from saathi.integrations import sarvam
from saathi.core.clock import clock
from saathi.mcp.mcp_client import MCPClient

LOCK = threading.RLock()


def reset_world():
    with LOCK:
        clock.reset()
        clock._listeners.clear()
        event_bus.clear()
        seed.seed()
        logger.clear()
        trace.clear()
        llm.reset_stats()
        clustering.invalidate()
        paytm_sim.install()
        event_bus.subscribe("sim.*", step6_verify.on_event)
        clock.on(step6_verify.on_clock)
        logger.log("clock", f"world reset · virtual clock {clock.now_iso()[:16]} IST · seed loaded · mock mode: "
                            f"sarvam={'live' if sarvam.is_live() else 'mock'} gemini={'live' if llm.is_live() else 'mock'}")


def set_case_status(case_id, status):
    c = store.load_case(case_id)
    if c:
        c["status"] = status
        store.save_case(c)


def _public(case):
    return {k: v for k, v in case.items() if not k.startswith("_")}


def _result(case, extra=None):
    out = {"case": _public(case), "trace": trace.get(case["id"]), "messages": [m for m in notify.thread(case["merchant_id"])
                                                                              if m["case_id"] == case["id"]]}
    out.update(extra or {})
    return out


# ------------------------------------------------------------------ entry points
_NUM_WORDS = {"zero": "0", "oh": "0", "o": "0", "nil": "0", "one": "1", "two": "2", "to": "2", "too": "2", "three": "3",
              "four": "4", "for": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_NUM_WORDS.update({w: d for d, ws in enumerate([
    "பூஜ்ஜியம் பூஜ்யம் ஜீரோ शून्य जीरो సున్నా",            # 0
    "ஒன்று ஒண்ணு एक ఒకటి",                               # 1
    "இரண்டு ரெண்டு दो రెండు",                            # 2
    "மூன்று மூணு तीन మూడు",                              # 3
    "நான்கு நாலு चार నాలుగు",                             # 4
    "ஐந்து அஞ்சு पांच पाँच ఐదు",                           # 5
    "ஆறு छह छः ఆరు",                                    # 6
    "ஏழு सात ఏడు",                                     # 7
    "எட்டு आठ ఎనిమిది",                                  # 8
    "ஒன்பது नौ తొమ్మిది"]) for w in ws.split()} | {})
_NUM_WORDS = {k: str(v) for k, v in _NUM_WORDS.items()}


def _digits_from_words(text):
    """'four two three one, double eight ...' -> '42318 8...' digits only."""
    out, mult = [], 1
    for w in re.findall(r"\d|[^\s\d.,;:!?-]+",(text or "").lower()):
        if w in ("double", "triple"):
            mult = 2 if w == "double" else 3
            continue
        d = w if w.isdigit() else _NUM_WORDS.get(w)
        if d is not None:
            out.append(d * mult)
        mult = 1
    return "".join(out)


def _seeded_scenario(prof, original):
    """Scenario id only when the transcript IS one of the seeded demo sentences; otherwise None (open world)."""
    t = (original or "").strip()
    for sid, sc in seed.SCENARIOS.items():
        if sc["merchant_id"] == prof["id"] and t in {v.strip() for v in sc["texts"].values()}:
            return sid
    return None


def handle_message(merchant_id, text=None, audio=None, audio_name=None, audio_mime=None, scenario_id=None, lang=None,
                   focus=False):
    """Chat first: greetings / thanks / status questions get a reply; only a complaint starts the 7 steps."""
    with LOCK:
        prof = step1_understand.DIRECTORY[merchant_id]
        lang = lang if lang in seed.LANGUAGES else prof["lang"]
        heard = None
        if audio:
            heard = sarvam.transcribe(audio, audio_name, audio_mime, scenario_id, merchant_id, lang)
            if not heard:
                raise step1_understand.CaseValidationError("could not understand the voice note, please type it")
            original, english = heard["text_original"], heard["text_english"]
        else:
            original = text
            english = conversation.seeded_english(text) or (text if step1_understand.detect_script(text) == "en" else None)
        msg_id = notify.record_inbound(merchant_id, None, original, english, lang)
        waiting = conversation.latest_case(merchant_id)
        utr = re.sub(r"\D", "", original or "")
        if waiting and waiting.get("status") == "AWAITING_UTR" and focus:
            # spoken numbers come back as words ("four two three ..."): turn them into digits
            said = _digits_from_words(english or "") or _digits_from_words(original or "")
            # demo focus: whatever the user replies while we wait for the UTR is taken as the demo payment's UTR
            logger.log("match", f"demo focus: reply while waiting for the UTR ({len(utr) or len(said)} digits heard)"
                                " → used as the UTR")
            utr = seed.SCENARIOS["S6"]["utr"]
        if waiting and waiting.get("status") == "AWAITING_UTR" and len(utr) == 12:
            notify.link_to_case(msg_id, waiting["id"])
            logger.log("match", f"12-digit number while {waiting['id']} waits for a UTR → continue that request")
            return _provide_utr(waiting, utr)
        conv = conversation.respond(merchant_id, prof["name"], lang, original, english, focus=focus)
        if conv["intent"] != "complaint":
            return {"case": None, "intent": conv["intent"], "trace": [], "messages": [conv["reply"]]}
        if focus and not (audio and not sarvam.is_live()):
            # the actual transcript is always what we work on; a seeded scenario is used only on an exact match
            sid = _seeded_scenario(prof, original)
            logger.log("match", f"transcript matches seeded {sid}" if sid else "no seeded match → open-world path")
            scenario_id = sid
        case = step1_understand.understand(merchant_id, text=None if heard else text, scenario_id=scenario_id, lang=lang,
                                           heard=heard)
        notify.link_to_case(msg_id, case["id"])
        return _continue(case)


def provide_utr(merchant_id, case_id, utr):
    with LOCK:
        case = store.load_case(case_id)
        if not case or case["merchant_id"] != merchant_id or case.get("status") != "AWAITING_UTR":
            raise PermissionError("no request of yours is waiting for a UTR")
        notify.record_inbound(merchant_id, case_id, f"UTR {utr}", f"UTR {utr}", case["lang"])
        return _provide_utr(case, re.sub(r"\D", "", utr))


def _provide_utr(case, utr):
    case["utr"] = utr
    case["status"] = "UNDERSTOOD"
    with logger.context(case_id=case["id"], step=1):
        logger.log("parse", f"UTR received for {case['id']} → look it up")
    return _continue(case)


def set_bank_late(on):
    with LOCK:
        paytm_sim.set_bank_late(on)
        return {"bank_late": bool(on)}


def choose(merchant_id, case_id, anomaly_id):
    with LOCK:
        case = store.load_case(case_id)
        if not case or case["merchant_id"] != merchant_id:
            raise PermissionError("case does not belong to this session")
        case = step1_understand.understand(merchant_id, choice=anomaly_id, case=case)
        return _continue(case)


def _continue(case):
    cid = case["id"]
    if case["status"] == "AWAITING_CHOICE":
        with logger.context(case_id=cid, step=1):
            notify.send(case["merchant_id"], cid, "choice", case["lang"], choices=case["choices"])
        return _result(case)
    dx = step2_diagnose.diagnose(case)
    case["diagnosis"] = dx
    case["llm_used"] = dx["llm_used"]
    mid, lang = case["merchant_id"], case["lang"]
    with logger.context(case_id=cid, step=2):
        if dx.get("await_input") == "utr":            # payment check: tell them now, then ask for the UTR
            hint = step1_understand.SCENARIOS["S6"]["utr"]
            kind = "utr_not_found" if dx.get("utr_not_found") else "ask_utr"
            notify.send(mid, cid, kind, lang, amount=f"{dx.get('amount') or 0:,}",
                        choices=[{"utr": hint, "label": f"UTR {hint[:4]} {hint[4:8]} {hint[8:]} (from the customer's screen)"}])
            case.update(status="AWAITING_UTR")
            store.save_case(case)
            return _result(case)
        if dx.get("answer"):
            notify.send_text(mid, cid, "answer", lang, notify.localise(dx["answer"], lang), dx["answer"])
            case.update(status="ANSWERED")
            store.save_case(case)
            return _result(case)
        if dx.get("explain"):                         # explain today's deductions; offer the fix for a flagged charge
            offer = dx.get("offer")
            choices = [{"anomaly_id": offer["id"], "label": f"Yes, fix the ₹{offer['amount']:,} rent charge ({offer['charge_id']})",
                        "rule": offer["rule"]}] if offer else None
            notify.send_text(mid, cid, "explain", lang, notify.localise(dx["explain"], lang), dx["explain"], choices=choices)
            case.update(status="AWAITING_CHOICE" if offer else "ANSWERED", choices=choices or [])
            store.save_case(case)
            return _result(case)
        if dx.get("guidance"):                        # failed UPI: what to do right now, before anything else
            notify.send(mid, cid, dx["guidance"]["kind"], lang, **dx["guidance"]["params"])
        if dx.get("notice"):
            notify.send(mid, cid, dx["notice"]["kind"], lang, **dx["notice"]["params"])
    if dx.get("needs_human") and not dx.get("fix"):
        with logger.context(case_id=cid, step=2):
            did = human_desk.open_item(cid, case["merchant_id"], "handoff", f"Needs a human: {dx.get('reason')}",
                                       {"diagnosis": step3_safety._dx_summary(dx), "text": case["text_english"]})
            notify.send(case["merchant_id"], cid, "handoff", case["lang"])
        case.update(status="NEEDS_HUMAN", desk_id=did)
        store.save_case(case)
        return _result(case)
    safety = step3_safety.safety_check(case, dx)
    token = safety.pop("token", None)
    case["safety"] = safety
    if safety["decision"] == "ALLOW":
        _act_and_contract(case, dx, token)
    elif safety["decision"] == "NEEDS_APPROVAL":
        with logger.context(case_id=cid, step=3):
            notify.send(case["merchant_id"], cid, "approval_wait", case["lang"])
        case.update(status="AWAITING_APPROVAL", desk_id=safety["desk_id"])
    else:
        with logger.context(case_id=cid, step=3):
            notify.send(case["merchant_id"], cid, "refusal", case["lang"])
        case.update(status="DENIED", desk_id=safety["desk_id"])
    store.save_case(case)
    return _result(case)


def _act_and_contract(case, dx, token):
    case["status"] = "ACTING"
    store.save_case(case)
    executed = step4_act.act(case, dx, token)
    case["executed"] = executed
    k = step5_contract.create(case, dx, executed)
    case.update(status="VERIFYING", contract_id=k["id"])
    store.save_case(case)


# ------------------------------------------------------------------ human desk
def desk_action(did, action, operator="ops.anita"):
    with LOCK:
        item = human_desk.get(did)
        if not item or item["status"] != "OPEN":
            raise ValueError(f"desk item {did} not open")
        case = store.load_case(item["case_id"])
        with logger.context(case_id=item["case_id"], step=7 if item["kind"] == "escalation" else 3):
            if item["kind"] == "approval" and action == "approve":
                human_desk.close_item(did, "approved", operator)
                dx = case["diagnosis"]
                logger.log("human", f"{operator} approved {'+'.join(a['action'] for a in dx['fix']['actions'])} for {case['id']}")
                token = step3_safety.issue_token(case["merchant_id"], case["id"], [a["action"] for a in dx["fix"]["actions"]],
                                                 max(a["amount"] for a in dx["fix"]["actions"]))
                trace.emit(case["id"], 3, "Human approval", "ok", f"approved by {operator} → token issued")
                case["approved_by"] = operator
                _act_and_contract(case, dx, token)
            elif action == "reject":
                human_desk.close_item(did, "rejected", operator)
                case["status"] = "REJECTED"
                store.save_case(case)
                notify.send(case["merchant_id"], case["id"], "handoff", case["lang"])
            elif item["kind"] == "escalation" and action == "resolve" and case["merchant_id"].startswith("C-"):
                logger.log("human", f"{operator} picks up {did}: calls the bank about the pending reversal")
                human_desk.close_item(did, "resolved: bank pushed the reversal", operator)
                paytm_sim.human_chase_bank(case["merchant_id"], operator)
            elif item["kind"] == "escalation" and action == "resolve":
                logger.log("human", f"{operator} picks up {did}: investigates {item['proof'].get('queue_items') and item['proof']['queue_items'][0]['id']}")
                human_desk.close_item(did, "resolved: finance queue unblocked", operator)
                paytm_sim.human_fix_credit_queue(case["merchant_id"], operator)
            elif item["kind"] == "playbook_review" and action == "approve":
                human_desk.close_item(did, "playbook approved", operator)
                learning.approve_playbook(item["proof"]["playbook"], operator)
            elif item["kind"] in ("blocked", "handoff") and action in ("resolve", "approve"):
                human_desk.close_item(did, "reviewed: refusal upheld" if item["kind"] == "blocked" else "handled by human", operator)
            else:
                raise ValueError(f"action {action} not valid for {item['kind']}")
        return {"desk": human_desk.get(did), "case": _public(store.load_case(item["case_id"]))}


# ------------------------------------------------------------------ clock
def advance(hours):
    with LOCK:
        logger.log("clock", f"advance +{hours:g}h requested")
        res = clock.advance(hours * 3600)
        logger.log("clock", f"now {res['to'][:16]} IST · {res['fired']} scheduled event(s) fired")
        return res


def tick():
    with LOCK:
        return clock.fire_due()


# ------------------------------------------------------------------ spike
def simulate_outage(n=40):
    with LOCK:
        t0 = time.perf_counter()
        held = paytm_sim.bank_outage(n)
        created, attached = [], 0
        with logger.muted():
            for h in held:
                client = MCPClient(h["merchant_id"], quiet=True)
                card = client.call("get_state_card", {})
                a = next(x for x in card["open_anomalies"] if x.get("settlement_id") == h["settlement_id"])
                key = anomaly_rules.incident_key(a)
                case = {"id": store.next_id("C-", 9917), "merchant_id": h["merchant_id"], "created_at": clock.now_iso(),
                        "status": "UNDERSTOOD", "issue_type": "settlement_held", "key": key, "anomaly": a, "source": "outage",
                        "lang": card["merchant"]["lang"], "text_english": "money not credited (outage ticket)", "priority": "NORMAL",
                        "match": {"status": "matched"}, "llm_used": "none"}
                inc = store.q1("SELECT id FROM incidents WHERE signature=? AND status='ACTIVE'", (key,))
                store.save_case(case)
                if inc:
                    learning.attach(inc["id"], case["id"], h["merchant_id"])
                    attached += 1
                else:
                    learning.detect_spikes()
                created.append(case["id"])
        ms = (time.perf_counter() - t0) * 1000
        logger.log("mcp-client", f"{len(held)} × get_state_card (per-call lines muted) · {ms:.0f} ms")
        opened = learning.detect_spikes()
        incs = store.q("SELECT incident_id, COUNT(*) n FROM incident_members WHERE case_id IN (%s) GROUP BY incident_id"
                       % ",".join("?" * len(created)), created)
        logger.log("incident", f"{len(created)} tickets → {len(incs)} incident(s) " + ", ".join(f"{i['incident_id']} ({i['n']})" for i in incs))
        return {"cases": len(created), "incidents": incs, "opened": opened}


# ------------------------------------------------------------------ metrics
def metrics():
    ks = store.q("SELECT status, closed_at, original_deadline FROM contracts")
    closed = [k for k in ks if k["status"] == "CLOSED"]
    decided = [k for k in ks if k["status"] == "CLOSED" or clock.now_iso() > k["original_deadline"]]
    kept = [k for k in closed if k["closed_at"] <= k["original_deadline"]]
    chat = store.q("SELECT llm_used, status FROM cases WHERE source='chat'")
    inc = store.q("SELECT incident_id, COUNT(*) n FROM incident_members GROUP BY incident_id")
    actioned = [c for c in chat if c["status"] in ("VERIFYING", "REMEDIATING", "CLOSED", "ESCALATED")]
    return {
        "verified_resolution_rate": {"num": len(closed), "den": len(ks), "label": "contracts closed by a passing check"},
        "promises_kept": {"num": len(kept), "den": len(decided), "label": "closed before the original deadline"},
        "gemini_calls": {"num": llm.STATS["calls"] + llm.STATS["mock_calls"], "den": len(chat),
                         "live": llm.STATS["calls"], "mock": llm.STATS["mock_calls"], "label": "Gemini calls vs total cases"},
        "llm_tiers": {t: sum(1 for c in chat if c["llm_used"] == t) for t in ("none", "tier1", "tier2")},
        "tickets_per_incident": {"num": sum(i["n"] for i in inc), "den": len(inc), "label": "tickets grouped per incident"},
        "cases": len(chat), "actioned": len(actioned),
        "autonomy_rule": learning.PROMOTION_RULE,
        "playbooks": store.q("SELECT id, name, autonomy, status, wins, attempts, source FROM playbooks ORDER BY created_at"),
    }
