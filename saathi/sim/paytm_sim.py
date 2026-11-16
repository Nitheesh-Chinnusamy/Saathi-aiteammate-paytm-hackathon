"""paytm_sim.py - mock Paytm systems of record (devices, billing, ledger, finance queue,
settlements, warehouse, payments) + their scheduled system events.

This is the "Paytm side" of the world: it owns and mutates the merchant data tables and
publishes domain events (sim.rental_stopped, sim.credit_posted, sim.settlement_credited,
sim.heartbeat ...) at virtual timestamps. Write requests arrive ONLY through mcp_server's
write tools. Every log line is tagged [sim].

Seeded behaviours: Ramesh's refund credit queue is STUCK (credit never posts until a human
fixes it), Lakshmi's credit posts +30h, settlement re-queue credits +6h, remote restart
brings the heartbeat back +15 min, Sunita's QR payments keep crediting while alerts fail.

Production: Paytm's real billing, ledger, settlement, device-management and warehouse services.
"""
import json
from datetime import timedelta

from saathi.core import event_bus
from saathi.core import logger
from saathi.sim import seed
from saathi.core import store
from saathi.core.clock import clock, iso, parse


def _log(msg, **f):
    logger.log("sim", f"[sim] {msg}", **f)


def _cfg(k):
    return seed.sim_config(k)


# ----------------------------------------------------------------- write operations (via MCP)
def stop_rental(merchant_id, device_id, idem_key):
    dev = store.q1("SELECT * FROM devices WHERE id=? AND merchant_id=?", (device_id, merchant_id))
    if not dev:
        return {"ok": False, "error": "device_not_found"}
    if dev["rental_status"] in ("STOP_REQUESTED", "STOPPED"):
        _log(f"billing: stop_rental {device_id} already {dev['rental_status']} (idem {idem_key})")
        return {"ok": True, "status": dev["rental_status"], "duplicate": True}
    store.update("devices", "id", device_id, rental_status="STOP_REQUESTED")
    hours = _cfg("rental_stop_hours")
    at = clock.schedule_in(hours * 3600, "rental_stopped", _on_rental_stopped,
                           {"merchant_id": merchant_id, "device_id": device_id})
    _log(f"billing: stop_rental {device_id} accepted · effective at next batch (+{hours}h)", device_id=device_id, at=at)
    return {"ok": True, "status": "STOP_REQUESTED", "effective_at": at}


def _on_rental_stopped(merchant_id, device_id):
    now = clock.now_iso()
    eid = store.next_id("BE-", 4410)
    store.update("devices", "id", device_id, rental_status="STOPPED", state="RENTAL_STOPPED", state_since=now)
    store.insert("billing_events", {"id": eid, "merchant_id": merchant_id, "device_id": device_id, "event": "RENTAL_STOPPED", "ts": now})
    _log(f"device {device_id} rental_stopped · {eid}")
    event_bus.publish("sim.rental_stopped", {"merchant_id": merchant_id, "device_id": device_id, "event_id": eid})


def post_credit(merchant_id, amount, ref, kind, idem_key):
    existing = store.q1("SELECT * FROM finance_queue WHERE idem_key=?", (idem_key,))
    if existing:
        _log(f"finance: credit {idem_key} already queued as {existing['id']} ({existing['status']}) → no duplicate")
        return {"ok": True, "queue_id": existing["id"], "status": existing["status"], "duplicate": True}
    qid = store.next_id("FQ-", 5501)
    store.insert("finance_queue", {"id": qid, "merchant_id": merchant_id, "amount": amount, "ref": ref, "kind": kind,
                                   "idem_key": idem_key, "status": "PENDING", "created_at": clock.now_iso()})
    if kind == "refund" and merchant_id in (_cfg("stuck_refund_queues") or []):
        _log(f"finance: {qid} ₹{amount} {kind} queued PENDING (refund queue for {merchant_id} is not draining)", queue_id=qid)
    else:
        hours = _cfg("goodwill_hours") if kind == "goodwill" else _cfg("credit_hours")
        at = clock.schedule_in(hours * 3600, "credit_posted", _post_from_queue, {"queue_id": qid})
        _log(f"finance: {qid} ₹{amount} {kind} queued PENDING · posts at next cycle (+{hours}h)", queue_id=qid, at=at)
    return {"ok": True, "queue_id": qid, "status": "PENDING"}


def _post_from_queue(queue_id, by="finance-batch"):
    row = store.q1("SELECT * FROM finance_queue WHERE id=?", (queue_id,))
    if not row or row["status"] == "POSTED":
        return None
    now = clock.now_iso()
    le = store.next_id("LE-", 8801)
    store.update("finance_queue", "id", queue_id, status="POSTED", posted_at=now)
    store.insert("ledger_entries", {"id": le, "merchant_id": row["merchant_id"], "type": "CREDIT", "amount": row["amount"],
                                    "ref": row["ref"], "ts": now, "status": "POSTED"})
    _log(f"ledger: {le} CREDIT ₹{row['amount']} ref {row['ref']} → {row['merchant_id']} ({by})")
    event_bus.publish("sim.credit_posted", {"merchant_id": row["merchant_id"], "event_id": le, "queue_id": queue_id,
                                            "amount": row["amount"], "ref": row["ref"]})
    return le


def requeue_settlement(merchant_id, settlement_id, idem_key):
    st = store.q1("SELECT * FROM settlements WHERE id=? AND merchant_id=?", (settlement_id, merchant_id))
    if not st:
        return {"ok": False, "error": "settlement_not_found"}
    if st["status"] in ("SENT_TO_BANK", "CREDITED"):
        _log(f"settlement {settlement_id} already {st['status']} (idem {idem_key})")
        return {"ok": True, "status": st["status"], "duplicate": True}
    store.update("settlements", "id", settlement_id, status="SENT_TO_BANK", reason=None)
    hours = _cfg("settlement_hours")
    at = clock.schedule_in(hours * 3600, "settlement_credited", _on_settlement_credited,
                           {"merchant_id": merchant_id, "settlement_id": settlement_id})
    _log(f"settlement {settlement_id} ₹{st['amount']} re-queued → SENT_TO_BANK via alternate rail · bank ack in ~{hours}h")
    return {"ok": True, "status": "SENT_TO_BANK", "expected_at": at}


def _on_settlement_credited(merchant_id, settlement_id):
    now = clock.now_iso()
    st = store.q1("SELECT * FROM settlements WHERE id=?", (settlement_id,))
    le = store.next_id("LE-", 8801)
    store.update("settlements", "id", settlement_id, status="CREDITED", credited_at=now)
    store.insert("ledger_entries", {"id": le, "merchant_id": merchant_id, "type": "SETTLEMENT", "amount": st["amount"],
                                    "ref": settlement_id, "ts": now, "status": "POSTED"})
    _log(f"bank ack: settlement {settlement_id} ₹{st['amount']} CREDITED · {le}")
    event_bus.publish("sim.settlement_credited", {"merchant_id": merchant_id, "settlement_id": settlement_id, "event_id": le})


def remote_restart(merchant_id, device_id, idem_key):
    dev = store.q1("SELECT * FROM devices WHERE id=? AND merchant_id=?", (device_id, merchant_id))
    if not dev:
        return {"ok": False, "error": "device_not_found"}
    mins = _cfg("restart_minutes")
    at = clock.schedule_in(mins * 60, "heartbeat", _on_heartbeat, {"merchant_id": merchant_id, "device_id": device_id})
    _log(f"device-mgmt: remote_restart {device_id} sent over SMS-wake · reboot ~{mins} min")
    return {"ok": True, "status": "RESTART_SENT", "expected_at": at}


def _on_heartbeat(merchant_id, device_id):
    now = clock.now_iso()
    hb = store.next_id("HB-", 5101)
    store.update("devices", "id", device_id, last_heartbeat=now, online=1)
    store.insert("heartbeats", {"id": hb, "device_id": device_id, "merchant_id": merchant_id, "ts": now})
    _log(f"device {device_id} heartbeat {hb} · online · alerts resumed")
    event_bus.publish("sim.heartbeat", {"merchant_id": merchant_id, "device_id": device_id, "event_id": hb})


# ----------------------------------------------------------------- compensations (registered, rarely run)
def resume_rental(merchant_id, device_id, idem_key=None):
    store.update("devices", "id", device_id, rental_status="ACTIVE")
    _log(f"billing: compensation resume_rental {device_id}")
    return {"ok": True}


def reverse_credit(merchant_id, ref, idem_key=None):
    store.ex("UPDATE finance_queue SET status='REVERSED' WHERE ref=? AND merchant_id=? AND status='PENDING'", (ref, merchant_id))
    _log(f"finance: compensation reverse_credit ref {ref}")
    return {"ok": True}


# ----------------------------------------------------------------- human-side fixes
def human_fix_credit_queue(merchant_id, operator):
    """A finance-ops human finds the stuck refund queue and pushes it through."""
    rows = store.q("SELECT id FROM finance_queue WHERE merchant_id=? AND status='PENDING'", (merchant_id,))
    stuck = _cfg("stuck_refund_queues") or []
    if merchant_id in stuck:
        stuck.remove(merchant_id)
        store.ex("UPDATE sim_config SET value=? WHERE key='stuck_refund_queues'", (json.dumps(stuck),))
    _log(f"finance: queue for {merchant_id} unblocked by {operator} · {len(rows)} pending item(s) released")
    return [_post_from_queue(r["id"], by=f"manual post by {operator}") for r in rows]


# ----------------------------------------------------------------- background world
def on_clock(kind, info):
    """QR payments for Sunita keep crediting every 2h of virtual time; alerts depend on device online."""
    if kind != "advanced":
        return
    start, end = parse(info["from"]), parse(info["to"])
    dev = store.q1("SELECT online FROM devices WHERE id='SB-91'")
    if not dev:
        return
    t, n = start + timedelta(hours=2), 0
    while t <= end:
        pid = store.next_id("PAY-", 7101)
        store.insert("payments", {"id": pid, "merchant_id": "M-5150", "amount": 50 + (n * 37) % 300, "ts": iso(t),
                                  "status": "CREDITED", "channel": "UPI_QR", "alert_sent": dev["online"]})
        t += timedelta(hours=2)
        n += 1
    if n:
        _log(f"{n} QR payment(s) credited to M-5150 · alerts {'sent' if dev['online'] else 'NOT sent (device offline)'}")


def bank_outage(n=40):
    """Deccan Co-op Bank stops responding: n settlements to DCCB merchants go HELD over the last 2h."""
    pool = store.q("SELECT id FROM merchants WHERE ifsc LIKE 'DCCB%' AND id != 'M-4420' ORDER BY id")
    now = clock.now()
    held = []
    for i in range(n):
        m = pool[i % len(pool)]["id"]
        sid = store.next_id("S-", 9500)
        ts = iso(now - timedelta(minutes=120 - i * 3))
        mer = store.q1("SELECT ifsc, bank_name FROM merchants WHERE id=?", (m,))
        store.insert("settlements", {"id": sid, "merchant_id": m, "amount": 2000 + (i * 733) % 9000, "status": "HELD",
                                     "reason": "BANK_TIMEOUT", "bank_name": mer["bank_name"], "ifsc": mer["ifsc"], "ts": ts})
        held.append({"merchant_id": m, "settlement_id": sid})
    _log(f"Deccan Co-op Bank outage: {n} settlements HELD (BANK_TIMEOUT) across {len({h['merchant_id'] for h in held})} merchants in 2h")
    return held


# ----------------------------------------------------------------- QR replacement (S6)
def qr_replacement(merchant_id, reason, idem_key):
    prior = store.q1("SELECT * FROM sim_requests WHERE idem_key=?", (idem_key,))
    if prior:
        _log(f"qr-ops: replacement {prior['id']} already raised (idem {idem_key})")
        return {"ok": True, "request_id": prior["id"], "duplicate": True}
    rid = store.next_id("QRR-", 401)
    store.insert("sim_requests", {"id": rid, "kind": "qr_replacement", "subject_id": merchant_id, "ref": reason,
                                  "idem_key": idem_key, "ts": clock.now_iso()})
    old = store.q("SELECT id FROM qr_codes WHERE merchant_id=? AND status='ACTIVE'", (merchant_id,))
    for o in old:
        store.update("qr_codes", "id", o["id"], status="BLOCKED")
    h1, h2 = _cfg("qr_register_hours"), _cfg("qr_test_payment_hours")
    clock.schedule_in(h1 * 3600, "qr_registered", _on_qr_registered, {"merchant_id": merchant_id, "request_id": rid})
    clock.schedule_in(h2 * 3600, "qr_test_payment", _on_qr_test_payment, {"merchant_id": merchant_id})
    _log(f"qr-ops: {rid} old QR {[o['id'] for o in old]} BLOCKED · field agent delivers new QR in ~{h1}h · test payment ~{h2}h")
    return {"ok": True, "request_id": rid, "status": "RAISED"}


def _on_qr_registered(merchant_id, request_id):
    qid = store.next_id("QR-", 7701)
    vpa = store.q1("SELECT linked_vpa FROM merchants WHERE id=?", (merchant_id,))["linked_vpa"]
    store.insert("qr_codes", {"id": qid, "merchant_id": merchant_id, "vpa": vpa, "status": "ACTIVE", "created_at": clock.now_iso()})
    _log(f"qr-ops: new QR {qid} registered to {merchant_id}'s own UPI ID ({request_id})")
    event_bus.publish("sim.qr_registered", {"merchant_id": merchant_id, "event_id": qid})


def _on_qr_test_payment(merchant_id):
    pid = store.next_id("PAY-", 7101)
    store.insert("payments", {"id": pid, "merchant_id": merchant_id, "amount": 1, "ts": clock.now_iso(), "status": "CREDITED",
                              "channel": "UPI_QR_TEST", "alert_sent": 1})
    _log(f"payments: ₹1 test payment {pid} on new QR credited to {merchant_id} · alert sent")
    event_bus.publish("sim.test_payment", {"merchant_id": merchant_id, "event_id": pid})


# ----------------------------------------------------------------- failed UPI refund (S7)
def raise_reversal_request(customer_id, utr, idem_key):
    txn = store.q1("SELECT * FROM upi_txns WHERE utr=? AND customer_id=?", (utr, customer_id))
    if not txn:
        return {"ok": False, "error": "txn_not_found"}
    prior = store.q1("SELECT * FROM sim_requests WHERE idem_key=?", (idem_key,))
    if prior:
        _log(f"upi-switch: reversal request {prior['id']} already raised (idem {idem_key})")
        return {"ok": True, "request_id": prior["id"], "duplicate": True}
    rid = store.next_id("RVR-", 301)
    store.insert("sim_requests", {"id": rid, "kind": "reversal_request", "subject_id": customer_id, "ref": txn["id"],
                                  "idem_key": idem_key, "ts": clock.now_iso()})
    _log(f"upi-switch: reversal request {rid} for {txn['id']} ₹{txn['amount']} raised with {txn['bank_name']}")
    return {"ok": True, "request_id": rid, "status": "RAISED"}


def post_customer_credit(customer_id, amount, ref, kind, idem_key):
    prior = store.q1("SELECT * FROM customer_credits WHERE idem_key=?", (idem_key,))
    if prior:
        _log(f"ledger: customer credit {prior['id']} already posted (idem {idem_key}) → no duplicate")
        return {"ok": True, "credit_id": prior["id"], "duplicate": True}
    cid = store.next_id("CC-", 601)
    store.insert("customer_credits", {"id": cid, "customer_id": customer_id, "amount": amount, "ref": ref, "kind": kind,
                                      "idem_key": idem_key, "ts": clock.now_iso()})
    _log(f"ledger: {cid} ₹{amount} {kind} credited to {customer_id} (ref {ref})")
    return {"ok": True, "credit_id": cid, "status": "POSTED"}


def _reversal_batch(txn_id, late_run=False):
    txn = store.q1("SELECT * FROM upi_txns WHERE id=?", (txn_id,))
    if not txn or txn["reversal_status"] != "PENDING":
        return
    if _cfg("bank_late") and not late_run:
        retry = parse(txn["ts"]).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=6)   # past T+5
        clock.schedule_at(retry, "reversal_batch_late", _reversal_batch, {"txn_id": txn_id, "late_run": True})
        _log(f"{txn['bank_name']}: reversal batch for {txn_id} FAILED (bank late) · next run {iso(retry)[:16]}")
        return
    _post_reversal(txn, by=f"{txn['bank_name']} reversal batch")


def _post_reversal(txn, by):
    ref = store.next_id("RVSL-", 9101)
    store.update("upi_txns", "id", txn["id"], reversal_status="REVERSED", reversal_ref=ref, reversal_at=clock.now_iso(),
                 status="REVERSED")
    _log(f"{txn['bank_name']}: ₹{txn['amount']} reversed to {txn['customer_id']} · {ref} ({by})")
    event_bus.publish("sim.reversal_posted", {"merchant_id": txn["customer_id"], "event_id": ref, "txn_id": txn["id"]})
    return ref


def human_chase_bank(customer_id, operator):
    rows = store.q("SELECT * FROM upi_txns WHERE customer_id=? AND reversal_status='PENDING'", (customer_id,))
    _log(f"bank-ops: {operator} escalated {len(rows)} pending reversal(s) for {customer_id} with the bank")
    return [_post_reversal(t, by=f"manual push by {operator}") for t in rows]


def set_bank_late(on):
    store.ex("UPDATE sim_config SET value=? WHERE key='bank_late'", (json.dumps(bool(on)),))
    _log(f"config: Deccan Co-op Bank reversal batch {'WILL BE LATE' if on else 'on time'} (demo switch)")


def install():
    clock.on(on_clock)
    # the bank's own reversal batch for failed UPI payments: T+1 at 18:00 (runs whether or not anyone complains)
    for t in store.q("SELECT id, ts FROM upi_txns WHERE reversal_status='PENDING'"):
        at = parse(t["ts"]).replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=1)
        clock.schedule_at(at, "reversal_batch", _reversal_batch, {"txn_id": t["id"]})
