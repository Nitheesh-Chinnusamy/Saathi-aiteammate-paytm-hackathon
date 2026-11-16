"""step4_act.py - Step 4 Act: the single action gateway.

* dry_run(): reads current state via MCP and returns the exact diff a change would make
  (consumed by Step 3's policy rules).
* execute(): verifies the HMAC token (merchant, action, amount, expiry), enforces idempotency
  (key = case_id + action; a retry never double-credits), registers the compensation BEFORE
  executing, calls the MCP write tool, appends to the hash-chained audit log, and publishes
  actions.executed.

Production: an action gateway service (Envoy/OPA sidecar) + an append-only ledger (QLDB/
immutable table) for audit; Temporal saga compensations.
"""
import hashlib
import json

from saathi.core import event_bus
from saathi.core import logger
from saathi.steps import step3_safety
from saathi.core import store
from saathi.core import trace
from saathi.core.clock import clock
from saathi.mcp.mcp_client import MCPClient

COMPENSATIONS = {"stop_rental": "resume_rental", "post_credit": "reverse_credit",
                 "requeue_settlement": "recall_settlement", "remote_restart": "none (idempotent reboot)",
                 "qr_replacement": "reactivate_old_qr", "raise_reversal_request": "withdraw_reversal_request",
                 "post_customer_credit": "reverse_customer_credit"}


class GatewayError(Exception):
    pass


# ------------------------------------------------------------------ audit chain
def audit_append(case_id, action, row):
    with store.LOCK:
        last = store.q1("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1")
        prev = last["hash"] if last else "GENESIS"
        row_json = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        h = hashlib.sha256((prev + row_json).encode()).hexdigest()
        seq = store.ex("INSERT INTO audit_log (ts, case_id, action, row_json, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                       (clock.now_iso(), case_id, action, row_json, prev, h))
    logger.log("audit", f"#{seq} {action} {case_id} · hash {h[:10]}… ← prev {prev[:10]}…")
    return seq


def verify_chain():
    rows = store.q("SELECT * FROM audit_log ORDER BY seq")
    prev = "GENESIS"
    for r in rows:
        h = hashlib.sha256((prev + r["row_json"]).encode()).hexdigest()
        if r["prev_hash"] != prev or r["hash"] != h:
            logger.log("audit", f"chain BROKEN at #{r['seq']} ({len(rows)} rows)", level="error")
            return {"ok": False, "broken_at": r["seq"], "rows": len(rows)}
        prev = r["hash"]
    logger.log("audit", f"chain verified · {len(rows)} rows · head {prev[:10]}…")
    return {"ok": True, "rows": len(rows), "head": prev}


# ------------------------------------------------------------------ dry run
def dry_run(merchant_id, actions, client=None):
    client = client or MCPClient(merchant_id)
    diff = []
    need = {a["action"] for a in actions}
    devices = {d["id"]: d for d in client.call("get_devices", {})["rows"]} if need & {"stop_rental", "remote_restart"} else {}
    charges = {c["id"]: c for c in client.call("get_charges", {"days": 90})["rows"]} if need & {"post_credit", "goodwill_credit"} else {}
    ledger = client.call("get_ledger_entries", {"days": 30})["rows"] if need & {"post_credit", "goodwill_credit", "payout"} else []
    credits_30d = [l for l in ledger if l["type"] == "CREDIT"]
    for a in actions:
        p, name = a["params"], a["action"]
        if name == "stop_rental":
            d = devices.get(p["device_id"])
            diff.append({"action": name, "target": p["device_id"], "owned": d is not None, "amount": 0,
                         "before": f"rental {d['rental_status']}" if d else "not found", "after": "rental STOPPED"})
        elif name in ("post_credit", "goodwill_credit"):
            ref = p["ref"]
            owned = ref in charges or ref.startswith("K-")
            diff.append({"action": name, "target": ref, "owned": owned, "amount": p["amount"],
                         "before": f"{len(credits_30d)} credit(s) in 30d", "after": f"ledger +₹{p['amount']} CREDIT ref {ref}"})
        elif name == "requeue_settlement":
            try:
                s = client.call("get_settlements", {"settlement_id": p["settlement_id"]})["rows"][0]
                owned, before = True, f"{s['status']} {s['reason'] or ''}".strip()
            except Exception:
                owned, before = False, "not found / not owned"
            diff.append({"action": name, "target": p["settlement_id"], "owned": owned, "amount": p.get("amount", 0),
                         "before": before, "after": "SENT_TO_BANK (re-queued)"})
        elif name == "remote_restart":
            d = devices.get(p["device_id"])
            diff.append({"action": name, "target": p["device_id"], "owned": d is not None, "amount": 0,
                         "before": f"online={d['online']}" if d else "not found", "after": "reboot → heartbeat"})
        elif name == "qr_replacement":
            qrs = [q for q in client.call("get_qr_codes", {})["rows"] if q["status"] == "ACTIVE"]
            diff.append({"action": name, "target": qrs[0]["id"] if qrs else "QR", "owned": bool(qrs), "amount": 0,
                         "before": f"QR {qrs[0]['id']} ACTIVE" if qrs else "no active QR", "after": "old QR blocked → new QR issued"})
        elif name == "raise_reversal_request":
            rows = client.call("get_upi_txns", {"utr": p["utr"]})["rows"]
            t = rows[0] if rows else None
            diff.append({"action": name, "target": t["id"] if t else p["utr"], "owned": bool(t), "amount": 0,
                         "before": f"{t['status']}, reversal {t['reversal_status']}" if t else "not found / not owned",
                         "after": "reversal request raised with the bank"})
        elif name == "rbi_compensation":
            diff.append({"action": name, "target": p["ref"], "owned": p["ref"].startswith("K-"), "amount": p["amount"],
                         "before": "—", "after": f"customer +₹{p['amount']} RBI TAT compensation"})
        elif name == "payout":
            diff.append({"action": name, "target": p["destination"], "owned": bool(p.get("linked")), "amount": p["amount"],
                         "before": "—", "after": f"₹{p['amount']:,} → {p['destination']} ({'linked' if p.get('linked') else 'EXTERNAL'})",
                         "linked": bool(p.get("linked"))})
        else:
            diff.append({"action": name, "target": "?", "owned": False, "amount": a.get("amount", 0), "before": "?", "after": "unknown action"})
    logger.log("gateway", "dry-run diff: " + " · ".join(f"{d['action']} {d['target']}: {d['before']} → {d['after']}" for d in diff))
    return {"diff": diff, "credits_30d": len(credits_30d)}


# ------------------------------------------------------------------ execute
TOOL_FOR = {"stop_rental": "stop_rental", "post_credit": "post_credit", "goodwill_credit": "post_credit",
            "requeue_settlement": "requeue_settlement", "remote_restart": "remote_restart", "qr_replacement": "qr_replacement",
            "raise_reversal_request": "raise_reversal_request", "rbi_compensation": "post_customer_credit"}


def execute(case_id, merchant_id, action, token, client=None):
    client = client or MCPClient(merchant_id)
    name = action["action"]
    tool = TOOL_FOR.get(name)
    if not tool:
        raise GatewayError(f"no executable tool for {name}")
    ok, reason = step3_safety.verify_token(token, merchant_id, tool, action.get("amount", 0))
    if not ok:
        logger.log("gateway", f"{name} REJECTED · token: {reason}", level="warn")
        raise GatewayError(reason)
    idem = f"{case_id}:{name}" + (f":{action['idem_suffix']}" if action.get("idem_suffix") else "")
    prior = store.q1("SELECT * FROM idempotency WHERE key=?", (idem,))
    if prior:
        logger.log("gateway", f"{name} idem={idem} seen before → replay stored result, no side effect")
        return {**json.loads(prior["result"]), "replay": True}
    comp = COMPENSATIONS.get(tool)
    store.insert("compensations", {"id": store.next_id("CP-", 101), "case_id": case_id, "action": name, "idem_key": idem,
                                   "compensation": comp, "params": action["params"], "status": "REGISTERED", "ts": clock.now_iso()})
    logger.log("gateway", f"compensation registered for {name}: {comp}")
    args = {k: v for k, v in action["params"].items() if k in ("device_id", "amount", "ref", "kind", "settlement_id", "utr", "reason")}
    res = client.call(tool, {**args, "gateway_token": token, "idempotency_key": idem})
    seq = audit_append(case_id, name, {"merchant_id": merchant_id, "action": name, "params": action["params"],
                                       "idem": idem, "result": res})
    out = {"action": name, "params": action["params"], "result": res, "audit_seq": seq, "idem": idem, "replay": False,
           "ts": clock.now_iso()}
    store.insert("idempotency", {"key": idem, "case_id": case_id, "action": name, "result": json.dumps(out, default=str),
                                 "ts": clock.now_iso()})
    amt = f" ₹{action['amount']:,}" if action.get("amount") else ""
    ref = f" ref {action['params'].get('ref') or action.get('target')}"
    logger.log("gateway", f"{name}{amt}{ref} idem={idem} ✓ audit#{seq}")
    event_bus.publish("actions.executed", {"case_id": case_id, "merchant_id": merchant_id, "action": name, "audit_seq": seq})
    return out


def act(case, dx, token):
    cid = case["id"]
    client = MCPClient(case["merchant_id"])
    results = []
    for a in dx["fix"]["actions"]:
        with trace.substep(cid, 4, f"Execute {a['action']}") as t:
            r = execute(cid, case["merchant_id"], a, token, client)
            results.append(r)
            t["result"] = f"{a['action']} ✓ · idem {r['idem']} · audit#{r['audit_seq']}" + (" (replay)" if r["replay"] else "")
            t["evidence"] = [f"audit#{r['audit_seq']}"]
    return results
