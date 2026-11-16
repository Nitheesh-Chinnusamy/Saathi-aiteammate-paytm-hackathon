"""step6_verify.py - Step 6 Verify: close a ticket ONLY when a check on the system of record passes.

Fixed, parameterised Python predicates (never LLM-written) read paytm_sim through MCP read
tools - NOT the gateway's response. Evaluated on every sim event (event-driven), on every
clock advance (reconcile() catches lost events) and by a deadline timer. All checks pass ->
CLOSED with a proof bundle (event/record IDs) + "done" message. Deadline passes with a check
failing -> Step 7 ladder.

Production: Kafka consumers + Temporal timers; predicates as versioned code in a registry.
"""

from saathi.core import logger
from saathi.services import notify
from saathi.core import store
from saathi.core import trace
from saathi.core.clock import clock, parse
from saathi.mcp.mcp_client import MCPClient


# ------------------------------------------------------------------ predicates (system of record)
def p_rental_stopped(client, device_id):
    d = next((x for x in client.call("get_devices", {})["rows"] if x["id"] == device_id), None)
    ev = [e for e in client.call("get_billing_events", {"device_id": device_id})["rows"] if e["event"] == "RENTAL_STOPPED"]
    ok = bool(d and d["rental_status"] == "STOPPED" and ev)
    return ok, ([ev[-1]["id"]] if ev else []), f"rental {d['rental_status'] if d else '?'}"


def p_credit_posted(client, amount, ref):
    rows = [r for r in client.call("get_ledger_entries", {"ref": ref})["rows"] if r["type"] == "CREDIT" and r["amount"] >= amount]
    return bool(rows), [r["id"] for r in rows], (f"ledger {rows[0]['id']} ₹{rows[0]['amount']}" if rows else "no ledger credit")


def p_settlement_credited(client, settlement_id):
    s = client.call("get_settlements", {"settlement_id": settlement_id})["rows"][0]
    le = [r for r in client.call("get_ledger_entries", {"ref": settlement_id})["rows"]] if s["status"] == "CREDITED" else []
    return bool(s["status"] == "CREDITED" and le), [x["id"] for x in le], f"settlement {s['status']}"


def p_heartbeat_received(client, device_id, after):
    hb = client.call("get_device_heartbeat", {"device_id": device_id})
    fresh = [r for r in hb["rows"] if parse(r["ts"]) > parse(after)]
    return bool(fresh and hb.get("online")), [r["id"] for r in fresh[:1]], f"online={hb.get('online')}"


def p_reversal_credited(client, utr):
    rows = client.call("get_upi_txns", {"utr": utr})["rows"]
    t = rows[0] if rows else None
    ok = bool(t and t["reversal_status"] == "REVERSED")
    return ok, ([t["reversal_ref"]] if ok else []), f"reversal {t['reversal_status'] if t else '?'}"


def p_qr_registered(client, after):
    rows = [q for q in client.call("get_qr_codes", {})["rows"] if q["status"] == "ACTIVE" and parse(q["created_at"]) > parse(after)]
    return bool(rows), [r["id"] for r in rows], (f"new QR {rows[0]['id']} active" if rows else "no new QR yet")


def p_test_payment_received(client, after):
    rows = [p for p in client.call("get_payments", {"hours": 48})["rows"]
            if p["channel"] == "UPI_QR_TEST" and parse(p["ts"]) > parse(after)]
    return bool(rows), [r["id"] for r in rows], (f"test payment {rows[0]['id']} credited" if rows else "no test payment yet")


PREDICATES = {"rental_stopped": p_rental_stopped, "credit_posted": p_credit_posted,
              "settlement_credited": p_settlement_credited, "heartbeat_received": p_heartbeat_received,
              "reversal_credited": p_reversal_credited, "qr_registered": p_qr_registered,
              "test_payment_received": p_test_payment_received}
LIVE = ("OPEN", "ESCALATED")   # escalated promises still close the moment the system of record shows the fix


def _save(k, **fields):
    store.update("contracts", "id", k["id"], **fields)


def evaluate(kid, trigger="reconcile", event_id=None):
    from saathi.steps import step5_contract
    k = step5_contract.load(kid)
    if not k or k["status"] not in LIVE:
        return k
    client = MCPClient(k["merchant_id"])
    with logger.context(case_id=k["case_id"], step=6):
        changed = False
        for c in k["checks"]:
            if c["status"] == "PASS":
                continue
            ok, ev, detail = PREDICATES[c["name"]](client, **c["params"])
            new = "PASS" if ok else "FAIL"
            if new != c["status"]:
                changed = True
                c.update(status=new, evidence=ev, detail=detail, checked_at=clock.now_iso(),
                         trigger=f"{trigger}{':' + event_id if event_id else ''}")
                n_ok = sum(x["status"] == "PASS" for x in k["checks"])
                logger.log("verify", f"{kid} check {c['name']} {'✓' if ok else '✗'} ({n_ok}/{len(k['checks'])}) · {detail}"
                                     + (f" · via {trigger} {event_id or ''}".rstrip() if trigger != "reconcile" else " · reconcile"))
                trace.emit(k["case_id"], 6, f"Check {c['name']}", "ok" if ok else "fail", f"{'✓' if ok else '✗'} {detail}", ev)
        _save(k, checks=k["checks"])
        if all(c["status"] == "PASS" for c in k["checks"]):
            return close(k)
        if clock.now() >= parse(k["deadline"]) and trigger != "remediate" and k["status"] == "OPEN":
            failing = [c["name"] for c in k["checks"] if c["status"] != "PASS"]
            logger.log("verify", f"{kid} deadline passed with {failing} failing → Step 7 ladder (cycle {k['cycle']})", level="warn")
            from saathi.steps import step7_remediate
            step7_remediate.remediate(kid)
        elif not changed and trigger == "event":
            logger.log("verify", f"{kid} re-checked on event {event_id}: no change")
    return step5_contract.load(kid)


def close(k):
    from saathi.services import learning
    from saathi.services import pipeline
    now = clock.now_iso()
    audit = [r["seq"] for r in store.q("SELECT seq FROM audit_log WHERE case_id=? ORDER BY seq", (k["case_id"],))]
    proof = {"checks": [{"name": c["name"], "evidence": c["evidence"], "detail": c.get("detail"), "at": c.get("checked_at"),
                         "trigger": c.get("trigger")} for c in k["checks"]],
             "audit_seqs": audit, "closed_at": now, "kept_original_deadline": parse(now) <= parse(k["original_deadline"]),
             "cycles": k["cycle"], "source_of_truth": "paytm_sim via MCP read tools"}
    _save(k, status="CLOSED", closed_at=now, proof=proof)
    ev_ids = [e for c in k["checks"] for e in c["evidence"]]
    logger.log("verify", f"{k['id']} all {len(k['checks'])} checks ✓ on system of record → CLOSED · proof {ev_ids} + audit {audit}")
    trace.emit(k["case_id"], 6, "Close with proof", "ok", f"CLOSED · proof {ev_ids}", ev_ids)
    with logger.context(case_id=k["case_id"], step=6):
        notify.send(k["merchant_id"], k["case_id"], k["promise"]["done_kind"], k["lang"], amount=f"{k['amount']:,}")
        pipeline.set_case_status(k["case_id"], "CLOSED")
        for d in store.q("SELECT id FROM desk WHERE case_id=? AND kind='escalation' AND status='OPEN'", (k["case_id"],)):
            from saathi.services import human_desk
            human_desk.close_item(d["id"], "auto-closed: verified on system of record", "saathi")
        learning.on_close(k["case_id"], k["id"])
    return k


def open_contracts():
    return [r["id"] for r in store.q("SELECT id FROM contracts WHERE status IN ('OPEN','ESCALATED') ORDER BY id")]


def on_event(topic, payload):
    mid = payload.get("merchant_id")
    for r in store.q("SELECT id FROM contracts WHERE status IN ('OPEN','ESCALATED') AND merchant_id=?", (mid,)):
        evaluate(r["id"], "event", payload.get("event_id"))


def on_deadline(kid, deadline):
    from saathi.steps import step5_contract
    k = step5_contract.load(kid)
    if k and k["status"] == "OPEN" and k["deadline"] == deadline:
        logger.log("clock", f"deadline timer fired for {kid}")
        evaluate(kid, "deadline")


def reconcile():
    ids = open_contracts()
    for kid in ids:
        evaluate(kid, "reconcile")
    return ids


def on_clock(kind, info):
    if kind == "advanced":
        ids = open_contracts()
        if ids:
            logger.log("verify", f"reconcile after clock advance → {len(ids)} open contract(s) {ids}")
        reconcile()
