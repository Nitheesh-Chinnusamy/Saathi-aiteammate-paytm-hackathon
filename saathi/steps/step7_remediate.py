"""step7_remediate.py - Step 7 If fail: the automatic remediation ladder, run in a fixed order.

  1 retry          re-send the same action through the gateway (same idempotency key -> no double credit)
  2 recheck        fresh read of the system of record + the finance queue (finds credit PENDING)
  3 chase_owner    owner ticket with evidence to the team that owns the cause
  4 compensate     goodwill ₹50 when a promise is missed (policy-checked + gateway, once per case)
  5 notify_delay   proactive delay message in the merchant's language with a new ETA
  6 escalate       human desk item with a proof pack
  7 rearm          new deadline (max 2 cycles), else ESCALATED
The ladder stops early if a step makes every check pass.
Note: for a failed UPI payment to a merchant, Saathi targets reversal by the end of T+1; the RBI limit for
merchant payments is T+5, and only after that is ₹100/day compensation due. Saathi's ₹50 goodwill is a
merchant-promise policy, not that rule.

Production: Temporal workflow with retries/compensation + PagerDuty/ops ticketing for owners.
"""
import json
from datetime import timedelta

from saathi.services import human_desk
from saathi.core import logger
from saathi.services import notify
from saathi.sim import seed
from saathi.steps import step3_safety
from saathi.steps import step4_act
from saathi.steps import step5_contract
from saathi.steps import step6_verify
from saathi.core import store
from saathi.core import trace
from saathi.core.clock import clock, iso, parse
from saathi.mcp.mcp_client import MCPClient

RBI_LIMIT_EXTRA_DAYS = 4          # target is end of T+1; RBI limit for merchant payments is end of T+5


def rbi_limit(k):
    return parse(k["original_deadline"]) + timedelta(days=RBI_LIMIT_EXTRA_DAYS)

CHECK_ACTION = {"credit_posted": "post_credit", "rental_stopped": "stop_rental", "settlement_credited": "requeue_settlement",
                "heartbeat_received": "remote_restart", "reversal_credited": "raise_reversal_request",
                "qr_registered": "qr_replacement", "test_payment_received": "qr_replacement"}
CHECK_OWNER = {"credit_posted": "finance-ops", "rental_stopped": "billing-ops", "settlement_credited": "settlement-ops",
               "heartbeat_received": "device-ops", "reversal_credited": "bank-ops (Deccan Co-op Bank)",
               "qr_registered": "field-ops", "test_payment_received": "field-ops"}


def _rbi_paid(cid):
    rows = store.q("SELECT result FROM idempotency WHERE case_id=? AND action='rbi_compensation'", (cid,))
    return sum(json.loads(r["result"])["params"]["amount"] for r in rows)


def _step(k, name, result, cid):
    k["ladder"].append({"step": name, "cycle": k["cycle"], "ts": clock.now_iso(), "result": result})
    store.update("contracts", "id", k["id"], ladder=k["ladder"])
    logger.log("remediate", f"{k['id']} {len(k['ladder'])}. {name}: {result}")
    trace.emit(cid, 7, name, "ok", result)


def _failing(k):
    return [c for c in k["checks"] if c["status"] != "PASS"]


def _all_pass(kid):
    k = step6_verify.evaluate(kid, "remediate")
    return k["status"] == "CLOSED", k


def remediate(kid):
    from saathi.services import pipeline
    k = step5_contract.load(kid)
    cid, mid = k["case_id"], k["merchant_id"]
    case = store.load_case(cid) or {}
    dx = case.get("diagnosis") or {}
    client = MCPClient(mid)
    store.update("contracts", "id", kid, status="REMEDIATING")
    pipeline.set_case_status(cid, "REMEDIATING")
    with logger.context(case_id=cid, step=7):
        failing = _failing(k)
        names = [c["name"] for c in failing]

        # 1 retry ---------------------------------------------------------
        acts = [a for a in (dx.get("fix") or {}).get("actions", []) if a["action"] in {CHECK_ACTION[n] for n in names}]
        if acts:
            tok = step3_safety.issue_token(mid, cid, [a["action"] for a in acts], max(a["amount"] for a in acts), ttl_min=5)
            res = [step4_act.execute(cid, mid, a, tok, client) for a in acts]
            _step(k, "retry", " · ".join(f"{r['action']} {'idempotent replay, no double credit' if r['replay'] else 'resent'}" for r in res), cid)
        else:
            _step(k, "retry", "no retryable action", cid)
        store.update("contracts", "id", kid, status="OPEN")
        done, k = _all_pass(kid)
        if done:
            return k
        store.update("contracts", "id", kid, status="REMEDIATING")

        # 2 quick re-check ---------------------------------------------------
        finding = "checks still failing on system of record"
        stuck = []
        if "credit_posted" in names:
            refs = {c["params"]["ref"] for c in failing if c["name"] == "credit_posted"}
            q = client.call("get_credit_queue", {})["rows"]
            stuck = [r for r in q if r["ref"] in refs and r["status"] == "PENDING"]
            if stuck:
                age = (clock.now() - parse(stuck[0]["created_at"])).total_seconds() / 3600
                finding = f"credit {stuck[0]['id']} ₹{stuck[0]['amount']} PENDING in finance queue for {age:.0f}h (expected P90 36h)"
        if "reversal_credited" in names:
            utr = next(c["params"]["utr"] for c in failing if c["name"] == "reversal_credited")
            t = client.call("get_upi_txns", {"utr": utr})["rows"][0]
            finding = f"{t['id']} still {t['status']} at {t['bank_name']} · reversal {t['reversal_status']} · past the T+1 target (RBI limit T+5 ends {rbi_limit(k).strftime('%d %b')})"
        _step(k, "recheck", finding, cid)

        # 3 chase owner -----------------------------------------------------
        owner = CHECK_OWNER.get(names[0], dx.get("owner") or "ops")
        ot = store.next_id("OT-", 71)
        evidence = {"contract": kid, "failing_checks": names, "queue_items": [s["id"] for s in stuck],
                    "diagnosis_evidence": [e["id"] for e in dx.get("evidence", [])]}
        store.insert("owner_tickets", {"id": ot, "case_id": cid, "owner": owner, "evidence": evidence, "status": "OPEN",
                                       "ts": clock.now_iso()})
        _step(k, "chase_owner", f"{ot} → {owner} with evidence {evidence['queue_items'] + evidence['diagnosis_evidence'][:3]}", cid)

        # 4 compensation --------------------------------------------------------
        amt = seed.policy("goodwill_amount")
        already = store.q1("SELECT key FROM idempotency WHERE key=?", (f"{cid}:goodwill_credit",))
        if k["playbook_id"] == "track_upi_reversal":
            # RBI TAT for merchant payments: ₹100 for every day the reversal is late beyond T+5
            per_day = seed.policy("rbi_comp_per_day")
            limit = rbi_limit(k)
            late_s = (clock.now() - limit).total_seconds()
            days = int(late_s // 86400) + 1 if late_s >= 0 else 0      # limit reached → day 1 is due
            due, paid = per_day * days, _rbi_paid(cid)
            pay = due - paid
            logger.log("remediate", f"RBI TAT: T+1 target missed · RBI limit (T+5) ends {iso(limit)[:16]} · "
                                    f"{days} day(s) past it × ₹{per_day}/day = ₹{due} due · ₹{paid} paid · paying ₹{max(pay, 0)}")
            if days == 0:
                _step(k, "compensate", f"within the RBI limit (T+5 ends {limit.strftime('%d %b')}): no compensation due yet", cid)
            elif pay > 0:
                action = {"action": "rbi_compensation", "params": {"amount": pay, "ref": f"{kid}:rbi:day{days}", "kind": "rbi_tat"},
                          "amount": pay, "target": f"{kid}:rbi", "idem_suffix": f"day{days}"}
                rdx = {"regulatory": True, "comp_limit": pay, "fix": {"playbook_id": None, "disputed_amount": None, "actions": [action]}}
                dry = step4_act.dry_run(mid, [action], client)
                rules = step3_safety.evaluate(case.get("merchant") or {"id": mid}, rdx, dry)
                decision = step3_safety.decide(rules)
                logger.log("policy", f"RBI compensation ₹{pay}: {sum(r['passed'] for r in rules)}/{len(rules)} rules ✓ → {decision}")
                if decision == "ALLOW":
                    tok = step3_safety.issue_token(mid, cid, ["rbi_compensation"], pay, ttl_min=5)
                    r = step4_act.execute(cid, mid, action, tok, client)
                    _step(k, "compensate", f"RBI ₹{per_day}/day × {days} day(s): paid ₹{pay} (total ₹{due}) · audit#{r['audit_seq']}", cid)
                else:
                    _step(k, "compensate", f"RBI compensation blocked by policy ({decision})", cid)
            else:
                _step(k, "compensate", f"RBI compensation already paid in full (₹{paid})", cid)
        elif already:
            _step(k, "compensate", "goodwill already paid for this case → skipped", cid)
        else:
            action = {"action": "goodwill_credit", "params": {"amount": amt, "ref": f"{kid}:goodwill", "kind": "goodwill"},
                      "amount": amt, "target": f"{kid}:goodwill"}
            gdx = {"goodwill": True, "fix": {"playbook_id": None, "disputed_amount": None, "actions": [action]}}
            dry = step4_act.dry_run(mid, [action], client)
            rules = step3_safety.evaluate(case.get("merchant") or {"id": mid}, gdx, dry)
            decision = step3_safety.decide(rules)
            logger.log("policy", f"goodwill ₹{amt}: {sum(r['passed'] for r in rules)}/{len(rules)} rules ✓ → {decision}")
            if decision == "ALLOW":
                tok = step3_safety.issue_token(mid, cid, ["goodwill_credit"], amt, ttl_min=5)
                r = step4_act.execute(cid, mid, action, tok, client)
                _step(k, "compensate", f"goodwill ₹{amt} for missed promise · audit#{r['audit_seq']}", cid)
            else:
                _step(k, "compensate", f"goodwill blocked by policy ({decision})", cid)

        # 5 proactive delay message ---------------------------------------------
        new_deadline = clock.now() + timedelta(hours=24)
        delay_kind = (k.get("promise") or {}).get("delay_kind", "delay")
        if k["playbook_id"] == "track_upi_reversal":
            if clock.now() < rbi_limit(k):
                new_deadline = rbi_limit(k)           # next checkpoint: the RBI limit itself
            if _rbi_paid(cid):
                delay_kind = "delay_reversal_comp"
        eta = new_deadline.strftime("%d %b %H:%M")
        notify.send(mid, cid, delay_kind, k["lang"], eta=eta, comp=f"{_rbi_paid(cid):,}")
        _step(k, "notify_delay", f"delay message [{k['lang']}] new ETA {eta}", cid)

        # 6 escalate ------------------------------------------------------------
        proof = {"contract": kid, "promise_deadline": k["original_deadline"], "checks": k["checks"], "ladder": k["ladder"],
                 "owner_ticket": ot, "queue_items": stuck, "diagnosis": {"cause": dx.get("cause"), "confidence": dx.get("confidence"),
                                                                         "evidence": [e["id"] for e in dx.get("evidence", [])]},
                 "audit": [r["seq"] for r in store.q("SELECT seq FROM audit_log WHERE case_id=?", (cid,))]}
        did = human_desk.open_item(cid, mid, "escalation", f"{kid}: {', '.join(names)} failing after deadline — {finding}", proof)
        _step(k, "escalate", f"human desk {did} with proof pack", cid)

        # 7 re-arm ----------------------------------------------------------------
        max_cycles = seed.policy("max_remediation_cycles")
        if k["cycle"] < max_cycles:
            nd = iso(new_deadline)
            store.update("contracts", "id", kid, status="OPEN", deadline=nd, cycle=k["cycle"] + 1)
            clock.schedule_at(nd, f"deadline {kid}", step6_verify.on_deadline, {"kid": kid, "deadline": nd})
            _step(k, "rearm", f"cycle {k['cycle'] + 1}/{max_cycles} · new deadline {eta}", cid)
            pipeline.set_case_status(cid, "VERIFYING")
        else:
            store.update("contracts", "id", kid, status="ESCALATED")
            _step(k, "rearm", f"max {max_cycles} cycles reached → ESCALATED (human owns it)", cid)
            pipeline.set_case_status(cid, "ESCALATED")
    return step5_contract.load(kid)
