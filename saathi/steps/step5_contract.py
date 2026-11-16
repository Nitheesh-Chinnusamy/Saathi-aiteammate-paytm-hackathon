"""step5_contract.py - Step 5 Contract: record a checkable promise with a deadline from data.

From the playbook template: promise text, postcondition NAMES (fixed, parameterised Python
predicates in step6_verify - never written by an LLM), deadline = P90 of historical verified
latency with a 25% buffer rounded up (credit: P90 36h -> 48h), and the on_fail ladder. The
promise message goes out in the merchant's language.

Production: contracts stored in Postgres; deadlines as Temporal durable timers.
"""
import math
from datetime import timedelta

from saathi.core.clock import parse

from saathi.core import logger
from saathi.services import notify
from saathi.steps import step2_diagnose
from saathi.steps import step6_verify
from saathi.core import store
from saathi.core import trace
from saathi.core.clock import clock, iso

LADDER = ["retry", "recheck", "chase_owner", "compensate", "notify_delay", "escalate", "rearm"]

TEMPLATES = {
    # playbook / action-set -> (checks builder, promise kind, done kind, latency issue_type)
    "stop_rental_and_credit": (lambda a: [{"name": "rental_stopped", "params": {"device_id": a["stop_rental"]["device_id"]}},
                                          {"name": "credit_posted", "params": {"amount": a["post_credit"]["amount"], "ref": a["post_credit"]["ref"]}}],
                               "promise_credit", "done_credit", "rent_after_return"),
    "requeue_settlement": (lambda a: [{"name": "settlement_credited", "params": {"settlement_id": a["requeue_settlement"]["settlement_id"]}}],
                           "promise_settlement", "done_settlement", "settlement_held"),
    "qr_replacement": (lambda a: [{"name": "qr_registered", "params": {"after": clock.now_iso()}},
                                  {"name": "test_payment_received", "params": {"after": clock.now_iso()}}],
                       "promise_qr", "done_qr", None),
    "track_upi_reversal": (lambda a: [{"name": "reversal_credited", "params": {"utr": a["raise_reversal_request"]["utr"]}}],
                           "promise_reversal", "done_reversal", None),
    "remote_restart": (lambda a: [{"name": "heartbeat_received", "params": {"device_id": a["remote_restart"]["device_id"],
                                                                            "after": clock.now_iso()}}],
                       "promise_restart", "done_restart", "device_offline"),
}


def deadline_hours(p90):
    """P90 * 1.25 buffer, rounded up to the hour (< 6h) or to the next 12h block."""
    x = p90 * 1.25
    return max(1, math.ceil(x)) if x <= 6 else int(math.ceil(x / 12.0) * 12)


def template_key(dx):
    if dx["fix"].get("playbook_id") in TEMPLATES:
        return dx["fix"]["playbook_id"]
    names = [a["action"] for a in dx["fix"]["actions"]]
    return names[0] if names and names[0] in TEMPLATES else None


def create(case, dx, executed):
    cid = case["id"]
    with trace.substep(cid, 5, "Contract + promise") as t:
        key = template_key(dx)
        build, promise_kind, done_kind, lat_issue = TEMPLATES[key]
        by_action = {a["action"]: a["params"] for a in dx["fix"]["actions"]}
        checks = [{**c, "status": "PENDING", "evidence": []} for c in build(by_action)]
        if key == "track_upi_reversal":          # target end of T+1; RBI limit for merchant (P2M) payments is T+5
            dl = parse(dx["fix"]["deadline"])
            hours = max(1, math.ceil((dl - clock.now()).total_seconds() / 3600))
            deadline, basis, p90 = iso(dl), "target: end of T+1 (RBI limit for merchant payments: T+5, then ₹100/day)", None
        else:
            p90 = 24 if key == "qr_replacement" else (step2_diagnose.p90_latency(lat_issue) or 24)
            hours = 24 if key == "qr_replacement" else deadline_hours(p90)
            deadline, basis = iso(clock.now() + timedelta(hours=hours)), ("QR SLA 24h" if key == "qr_replacement" else f"P90 {p90:g}h")
        kid =store.next_id("K-", 301)
        amount = dx["fix"].get("disputed_amount") or 0
        lang = case["lang"]
        delay_kind = "delay_reversal" if key == "track_upi_reversal" else "delay"
        store.insert("contracts", {"id": kid, "case_id": cid, "merchant_id": case["merchant_id"], "playbook_id": key,
                                   "checks": checks, "promise": {"kind": promise_kind, "done_kind": done_kind, "hours": hours,
                                                                 "delay_kind": delay_kind, "basis": basis},
                                   "deadline": deadline, "original_deadline": deadline, "status": "OPEN", "cycle": 1,
                                   "created_at": clock.now_iso(), "on_fail": LADDER, "lang": lang, "amount": amount,
                                   "ladder": []})
        clock.schedule_at(deadline, f"deadline {kid}", step6_verify.on_deadline, {"kid": kid, "deadline": deadline})
        logger.log("contract", f"{kid} · {len(checks)} checks [{', '.join(c['name'] for c in checks)}] · deadline +{hours}h ({basis})")
        msg = notify.send(case["merchant_id"], cid, promise_kind, lang, amount=f"{amount:,}", hours=hours,
                          deadline=step2_diagnose.deadline_label(parse(deadline)))
        t["result"] = f"{kid} · {len(checks)} checks · deadline +{hours}h ({basis}) · promise sent [{lang}]"
        t["evidence"] = [kid]
    case["contract_id"] = kid
    return {"id": kid, "checks": checks, "deadline": deadline, "hours": hours, "p90": p90, "message": msg}


def load(kid):
    r = store.q1("SELECT * FROM contracts WHERE id=?", (kid,))
    if r:
        for k in ("checks", "promise", "ladder", "on_fail", "proof"):
            r[k] = store.jload(r[k], r[k])
    return r


def all_contracts():
    rows = store.q("SELECT id FROM contracts ORDER BY created_at DESC, id DESC")
    return [load(r["id"]) for r in rows]
