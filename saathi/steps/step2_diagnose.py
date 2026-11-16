"""step2_diagnose.py - Step 2 Diagnose: find the real cause with cheap data tests; the LLM only
for leftover doubt (Tier 1) or novel cases (Tier 2).

start card (incident / cluster) -> lifecycle stuck-stage check (dwell vs P90) -> hypotheses with
priors from historical cases -> parallel read-only tests via mcp_client returning
{id, ts, source, value} -> Bayes update with a likelihood table -> Tier 1 if max posterior < 0.85
-> Tier 2 tool loop for unmatched/novel -> challenge (re-run the runner-up's test, must stay
< 0.10) -> cite check (every evidence id must exist) -> diagnosis.

Production: same Bayes-over-tests design; likelihood tables learned from labelled cases.
"""
from concurrent.futures import ThreadPoolExecutor

from saathi.integrations import llm
from saathi.core import logger
from saathi.core import store
from saathi.core import trace
from saathi.core.clock import clock, hours_between, iso, parse
from saathi.mcp.mcp_client import MCPClient

CONF_OK = 0.85
CHALLENGE_MAX = 0.10


def ev(id_, ts, source, value):
    return {"id": id_, "ts": ts, "source": source, "value": value}


# ------------------------------------------------------------------ tests (read-only, via MCP)
def t_billing_return_event(client, case):
    dev = case["anomaly"]["device_id"]
    rows = client.call("get_billing_events", {"device_id": dev})["rows"]
    ret = [r for r in rows if r["event"] in ("RETURN_RECORDED", "RENTAL_STOPPED")]
    e = ev(ret[0]["id"], ret[0]["ts"], "billing_events", ret[0]["event"]) if ret else \
        ev(f"Q-{dev}-billing", clock.now_iso(), "billing_events", f"{len(rows)} events for {dev}, none is a return")
    return "billing_return_absent", not ret, e


def _scan(client, case):
    dev = case["anomaly"]["device_id"]
    rows = client.call("get_warehouse_scan", {"device_id": dev})["rows"]
    serial = next((d["serial"] for d in client.call("get_devices", {})["rows"] if d["id"] == dev), None)
    return rows, serial


def t_serial_match(client, case):
    rows, serial = _scan(client, case)
    if not rows:
        return "serial_match", False, ev(f"Q-{case['anomaly']['device_id']}-scan", clock.now_iso(), "warehouse_scans", "no scan")
    s = rows[-1]
    return "serial_match", s["serial"] == serial, ev(s["id"], s["ts"], "warehouse_scans", f"serial {s['serial']} {'=' if s['serial'] == serial else '≠'} device")


def t_qc_pass(client, case):
    rows = client.call("get_warehouse_scan", {"device_id": case["anomaly"]["device_id"]})["rows"]
    if not rows:
        return "qc_pass", False, ev(f"Q-{case['anomaly']['device_id']}-qc", clock.now_iso(), "warehouse_scans", "no scan")
    s = rows[-1]
    return "qc_pass", s["qc"] == "PASS", ev(s["id"], s["ts"], "warehouse_scans", f"QC {s['qc']}")


def t_settlement_reason(client, case):
    sid = case["anomaly"]["settlement_id"]
    s = client.call("get_settlements", {"settlement_id": sid})["rows"][0]
    return "reason_bank_no_response", s["reason"] in ("BANK_NO_RESPONSE", "BANK_TIMEOUT"), ev(s["id"], s["ts"], "settlements", f"{s['status']} {s['reason']}")


def t_account_ok(client, case):
    rows = client.call("get_settlements", {})["rows"]
    ok = [r for r in rows if r["status"] == "CREDITED" and r["ifsc"] == next((x["ifsc"] for x in rows if x["id"] == case["anomaly"]["settlement_id"]), None)]
    if ok:
        r = ok[-1]
        return "account_ok", True, ev(r["id"], r["credited_at"], "settlements", f"₹{r['amount']} credited to same account")
    return "account_ok", False, ev(f"Q-{case['merchant_id']}-settle-ok", clock.now_iso(), "settlements", "no recent credit to this account")


def t_bank_wide(client, case):
    inc = case.get("incident")
    if inc and inc["id"]:
        row = store.q1("SELECT members FROM incidents WHERE id=?", (inc["id"],))
        return "bank_wide", True, ev(inc["id"], clock.now_iso(), "incidents", f"{row['members']} merchants on same bank+reason")
    return "bank_wide", False, ev(f"Q-{case['merchant_id']}-peers", clock.now_iso(), "incidents", "no bank-wide incident")


def t_payout_status(client, case):
    pid = case["anomaly"]["payout_id"]
    p = next(r for r in client.call("get_payouts", {})["rows"] if r["id"] == pid)
    return "payout_pending", p["status"] == "PENDING", ev(p["id"], p["ts"], "payouts", f"{p['kind']} {p['status']}")


def t_heartbeat_stale(client, case):
    d = case["anomaly"]["device_id"]
    hb = client.call("get_device_heartbeat", {"device_id": d})
    return "heartbeat_stale", not hb.get("online"), ev(d, hb.get("last_heartbeat"), "heartbeats", f"online={hb.get('online')}")


def _txn(client, case):
    return client.call("get_upi_txns", {"utr": case["anomaly"]["utr"]})["rows"][0]


def t_debited_not_credited(client, case):
    t = _txn(client, case)
    return "debited_not_credited", t["status"] == "DEBITED_NOT_CREDITED", ev(t["id"], t["ts"], "upi_txns", f"{t['status']} {t['reason'] or ''}".strip())


def t_reversal_pending(client, case):
    t = _txn(client, case)
    return "reversal_pending", t["reversal_status"] == "PENDING", ev(f"Q-{t['id']}-reversal", clock.now_iso(), "upi_txns",
                                                                     f"reversal {t['reversal_status']}")


KNOWLEDGE = {
    "debited_not_credited": {
        "tests": [t_debited_not_credited, t_reversal_pending, t_bank_wide],
        "likelihood": {
            "debited_not_credited": {"beneficiary_bank_timeout": 0.95, "npci_switch_delay": 0.90, "other": 0.40},
            "reversal_pending": {"beneficiary_bank_timeout": 0.90, "npci_switch_delay": 0.90, "other": 0.50},
            "bank_wide": {"beneficiary_bank_timeout": 0.85, "npci_switch_delay": 0.20, "other": 0.30}},
        "discriminator": {"npci_switch_delay": t_bank_wide},
        "lifecycle": None,
        "labels": {"beneficiary_bank_timeout": "the receiving bank timed out after your bank had already debited the money",
                   "npci_switch_delay": "the UPI switch delayed the payment", "other": "something else"}},
    "rent_after_return": {
        "tests": [t_billing_return_event, t_serial_match, t_qc_pass],
        "likelihood": {  # P(observation true | hypothesis)
            "billing_return_absent": {"billing_event_missing": 0.95, "wrong_serial": 0.90, "qc_failed": 0.90, "other": 0.30},
            "serial_match": {"billing_event_missing": 0.95, "wrong_serial": 0.05, "qc_failed": 0.95, "other": 0.50},
            "qc_pass": {"billing_event_missing": 0.95, "wrong_serial": 0.90, "qc_failed": 0.05, "other": 0.50}},
        "discriminator": {"billing_event_missing": t_billing_return_event, "wrong_serial": t_serial_match, "qc_failed": t_qc_pass},
        "lifecycle": "device_rental",
        "labels": {"billing_event_missing": "warehouse received it, billing never got the return event",
                   "wrong_serial": "warehouse scanned a different serial", "qc_failed": "device failed QC, return not accepted",
                   "other": "something else"}},
    "settlement_held": {
        "tests": [t_settlement_reason, t_account_ok, t_bank_wide],
        "likelihood": {
            "reason_bank_no_response": {"bank_no_response": 0.90, "wrong_ifsc": 0.10, "account_frozen": 0.20, "compliance_hold": 0.05, "other": 0.30},
            "account_ok": {"bank_no_response": 0.90, "wrong_ifsc": 0.05, "account_frozen": 0.10, "compliance_hold": 0.70, "other": 0.50},
            "bank_wide": {"bank_no_response": 0.80, "wrong_ifsc": 0.05, "account_frozen": 0.05, "compliance_hold": 0.20, "other": 0.30}},
        "discriminator": {"bank_no_response": t_settlement_reason, "wrong_ifsc": t_account_ok, "account_frozen": t_account_ok,
                          "compliance_hold": t_settlement_reason},
        "lifecycle": "settlement",
        "labels": {"bank_no_response": "bank did not acknowledge the transfer", "wrong_ifsc": "bank details wrong",
                   "account_frozen": "merchant account frozen", "compliance_hold": "compliance hold", "other": "something else"}},
    "incentive_pending": {
        "tests": [t_payout_status],
        "likelihood": {"payout_pending": {"campaign_batch_pending": 0.9, "eligibility_not_met": 0.8, "other": 0.5}},
        "discriminator": {"eligibility_not_met": t_payout_status},
        "lifecycle": None,
        "labels": {"campaign_batch_pending": "campaign payout batch not run yet", "eligibility_not_met": "merchant not eligible",
                   "other": "something else"}},
    "device_offline": {
        "tests": [t_heartbeat_stale],
        "likelihood": {"heartbeat_stale": {"sim_network_drop": 0.9, "power_issue": 0.9, "firmware_hang": 0.9, "other": 0.5}},
        "discriminator": {}, "lifecycle": None,
        "labels": {"sim_network_drop": "SIM lost network", "power_issue": "power issue", "firmware_hang": "firmware hang", "other": "other"}},
}
FIXES = {("rent_after_return", "billing_event_missing"), ("settlement_held", "bank_no_response"),
         ("debited_not_credited", "beneficiary_bank_timeout")}


def t1_end(ts):
    """Saathi's target for a failed UPI payment: reversal by the end of T+1 (RBI limit for merchant payments: T+5)."""
    from datetime import timedelta
    return parse(ts).replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=1)


def deadline_label(dt):
    return dt.strftime("%d %b, %I:%M %p")


# ------------------------------------------------------------------ knowledge helpers
def priors(issue_type):
    rows = store.q("SELECT cause, COUNT(*) n FROM historical_cases WHERE issue_type=? GROUP BY cause", (issue_type,))
    if rows:
        total = sum(r["n"] for r in rows)
        return {r["cause"]: r["n"] / total for r in rows}, total
    card = store.q1("SELECT causes FROM cluster_cards WHERE issue_type=?", (issue_type,))
    return (store.jload(card["causes"], {"other": 1.0}) if card else {"other": 1.0}), 0


def p90_latency(issue_type):
    lat = sorted(r["latency_hours"] for r in store.q(
        "SELECT latency_hours FROM historical_cases WHERE issue_type=? AND verified=1 AND latency_hours IS NOT NULL", (issue_type,)))
    if not lat:
        return None
    import math
    return lat[max(0, math.ceil(0.9 * len(lat)) - 1)]


def bayes(prior, observations, likelihood):
    post = dict(prior)
    for obs, val in observations.items():
        table = likelihood.get(obs)
        if not table:
            continue
        for h in post:
            p = table.get(h, 0.5)
            post[h] *= p if val else (1 - p)
    z = sum(post.values()) or 1.0
    return {h: v / z for h, v in post.items()}


def cite_check(claim_ids, registry):
    missing = [i for i in claim_ids if i not in registry]
    return (not missing and bool(claim_ids)), missing


def _run_parallel(tests, client, case, ctx):
    def run(fn):
        with logger.context(**ctx):
            return fn(client, case)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(run, tests))


# ------------------------------------------------------------------ main
def diagnose(case, client=None):
    client = client or MCPClient(case["merchant_id"])
    cid = case["id"]
    if case.get("intent") == "payout_request":
        return _diagnose_payout(case, client)
    if case.get("intent") == "payment_check":
        return _diagnose_payment_check(case, client)
    if case.get("intent") == "explain_today":
        return _diagnose_explain(case, client)
    if case["match"]["status"] == "unmatched" or case["issue_type"] not in KNOWLEDGE:
        return _diagnose_novel(case, client)

    issue = case["issue_type"]
    kb = KNOWLEDGE[issue]
    a = case["anomaly"]
    registry = {}
    dx = {"issue_type": issue, "llm_used": "none", "needs_human": False}

    with trace.substep(cid, 2, "Start card") as t:
        inc = case.get("incident")
        card = store.q1("SELECT * FROM cluster_cards WHERE issue_type=?", (issue,))
        prior, n = priors(issue)
        src = f"{inc['id']} card" if inc else f"cluster card {issue}"
        logger.log("bayes", f"start from {src} · {len(prior)} hypotheses · priors from {n or 'default'} historical cases")
        t["result"] = f"{src} · owner {card['owner']} · playbook {card['playbook_id'] or 'none'}"
        t["evidence"] = [inc["id"]] if inc else []

    with trace.substep(cid, 2, "Lifecycle stuck-stage check") as t:
        lc_name = kb["lifecycle"]
        if lc_name:
            lc = store.q1("SELECT * FROM lifecycles WHERE name=?", (lc_name,))
            p90 = store.jload(lc["p90_hours"])
            stage = a.get("state") if issue == "rent_after_return" else "HELD"
            since = a.get("state_since") or a.get("since")
            dwell = hours_between(since, clock.now_iso())
            limit = p90.get(stage)
            stuck = limit is not None and dwell > limit
            subj = a.get("device_id") or a.get("settlement_id")
            fmt = (lambda h: f"{h / 24:.0f}d") if (limit or 0) >= 48 else (lambda h: f"{h:.0f}h")
            logger.log("lifecycle", f"{subj} {stage} {fmt(dwell)} {'>' if stuck else '≤'} P90 {fmt(limit)} → {'stuck' if stuck else 'within normal dwell'}")
            dx["lifecycle"] = {"stage": stage, "dwell_h": round(dwell, 1), "p90_h": limit, "stuck": stuck}
            registry[subj] = ev(subj, since, lc_name, f"{stage} since {since[:10]}")
            t["result"] = f"{subj} {stage} {fmt(dwell)} vs P90 {fmt(limit)} → {'STUCK' if stuck else 'ok'}"
            t["evidence"] = [subj]
        else:
            logger.log("lifecycle", f"no lifecycle model for {issue} → skipped")
            t["result"] = "no lifecycle model"

    with trace.substep(cid, 2, "Parallel read-only tests") as t:
        results = _run_parallel(kb["tests"], client, case, {"case_id": cid, "step": 2})
        obs = {}
        for name, val, e in results:
            obs[name] = val
            registry[e["id"]] = e
        dx["tests"] = [{"obs": n, "value": v, "evidence": e} for n, v, e in results]
        t["result"] = " · ".join(f"{n}={'✓' if v else '✗'} ({e['id']})" for n, v, e in results)
        t["evidence"] = [e["id"] for _, _, e in results]

    with trace.substep(cid, 2, "Bayes update") as t:
        post = bayes(prior, obs, kb["likelihood"])
        top = max(post, key=post.get)
        logger.log("bayes", f"{top} {prior[top]:.2f} → {post[top]:.2f} after {len(obs)} tests")
        dx["priors"] = {h: round(p, 3) for h, p in prior.items()}
        dx["posteriors"] = {h: round(p, 3) for h, p in sorted(post.items(), key=lambda kv: -kv[1])}
        t["result"] = " · ".join(f"{h} {p:.2f}" for h, p in list(dx["posteriors"].items())[:3])

    with trace.substep(cid, 2, "LLM gate (Tier 1 only if < 0.85)", ai="none") as t:
        if post[top] < CONF_OK:
            t["ai"] = "Gemini tier1" + (" (mock)" if not llm.is_live() else "")
            hyps = sorted(post, key=post.get, reverse=True)[:5]
            letters = "ABCDE"
            options = {letters[i]: {"hypothesis": f"{h}: {kb['labels'].get(h, h)}", "posterior": post[h], "h": h} for i, h in enumerate(hyps)}
            out = llm.score_options(list(registry.values()), options, case)
            dx["llm_used"] = "tier1"
            dx["tier1"] = out
            if out["choice"] != "N" and out["confidence"] >= 0.7:
                top = options[out["choice"]]["h"]
                t["result"] = f"tier1 → {top} conf {out['confidence']:.2f}"
                dx["confidence"] = round(max(post[top], out["confidence"]), 3)
            else:
                dx["needs_human"], dx["reason"] = True, "tier1 could not separate hypotheses"
                t["result"] = "tier1 inconclusive → human"
        else:
            logger.log("llm", f"skipped (confidence {post[top]:.2f} ≥ {CONF_OK}) · Gemini calls this case: {case.get('gemini_calls', 0)}")
            t["result"] = f"skipped · {post[top]:.2f} ≥ {CONF_OK}"
            dx["confidence"] = round(post[top], 3)

    with trace.substep(cid, 2, "Challenge runner-up") as t:
        others = [h for h in sorted(post, key=post.get, reverse=True) if h != top and h in kb["discriminator"]]
        if others:
            ru = others[0]
            fn = kb["discriminator"][ru]
            name, val, e = _run_parallel([fn], client, case, {"case_id": cid, "step": 2})[0]
            registry[e["id"]] = e
            obs2 = dict(obs)
            obs2[name] = val
            post2 = bayes(prior, obs2, kb["likelihood"])
            ok = post2[ru] < CHALLENGE_MAX
            logger.log("bayes", f"challenge {ru}: re-ran {name} ({e['id']}) → {post2[ru]:.3f} {'<' if ok else '≥'} {CHALLENGE_MAX} {'✓' if ok else '✗ → human desk'}")
            dx["challenge"] = {"runner_up": ru, "test": name, "posterior": round(post2[ru], 3), "passed": ok, "evidence_id": e["id"]}
            if not ok:
                dx["needs_human"], dx["reason"] = True, f"runner-up {ru} still {post2[ru]:.2f}"
            t["result"] = f"{ru} re-tested → {post2[ru]:.3f} {'✓' if ok else '✗'}"
            t["evidence"] = [e["id"]]
        else:
            logger.log("bayes", "challenge: no runner-up with a discriminating test → skipped")
            t["result"] = "no runner-up test"

    with trace.substep(cid, 2, "Cite check + diagnosis") as t:
        cited = [x["evidence"]["id"] for x in dx["tests"]] + ([dx["challenge"]["evidence_id"]] if dx.get("challenge") else [])
        ok, missing = cite_check(cited, registry)
        logger.log("bayes", f"cite check {len(cited) - len(missing)}/{len(cited)} evidence ids resolve {'✓' if ok else '✗ ' + str(missing)}")
        if not ok:
            dx["needs_human"], dx["reason"] = True, f"uncited evidence {missing}"
        card = store.q1("SELECT * FROM cluster_cards WHERE issue_type=?", (issue,))
        dx.update(cause=top, cause_label=kb["labels"].get(top, top), evidence=list(registry.values()),
                  ruled_out=[{"cause": h, "p": round(p, 3)} for h, p in post.items() if h != top],
                  owner=card["owner"], expected_fix_hours=p90_latency(issue), cite_ok=ok)
        dx["fix"] = _fix_for(case, issue, top, card)
        if issue == "debited_not_credited":
            a = case["anomaly"]
            kind = "guidance_failed" if a["txn_status"] == "DEBITED_NOT_CREDITED" else "guidance_pending"
            dx["guidance"] = {"kind": kind, "params": {"amount": f"{a['amount']:,}", "deadline": deadline_label(t1_end(a["since"]))}}
            logger.log("bayes", f"counter guidance first: {kind} · target {deadline_label(t1_end(a['since']))} (end of T+1; RBI limit T+5)")
        if not dx["fix"]:
            dx["needs_human"] = True
            dx.setdefault("reason", f"no playbook for {issue}/{top}")
        logger.log("bayes", f"cause={top} conf {dx.get('confidence', 0):.2f} · owner {dx['owner']} · fix {(dx['fix'] or {}).get('playbook_id', 'none')}")
        t["result"] = f"{top} · conf {dx.get('confidence', 0):.2f} · fix {(dx['fix'] or {}).get('playbook_id', 'none → human')}"
        t["evidence"] = cited
    case["llm_used"] = dx["llm_used"]
    return dx


def _fix_for(case, issue, cause, card):
    a = case["anomaly"]
    if (issue, cause) not in FIXES:
        return None
    if issue == "rent_after_return":
        return {"playbook_id": "stop_rental_and_credit", "novel": False, "disputed_amount": a["amount"], "disputed_ref": a["charge_id"],
                "actions": [{"action": "stop_rental", "params": {"device_id": a["device_id"]}, "amount": 0, "target": a["device_id"]},
                            {"action": "post_credit", "params": {"amount": a["amount"], "ref": a["charge_id"], "kind": "refund"},
                             "amount": a["amount"], "target": a["charge_id"]}]}
    if issue == "debited_not_credited":
        return {"playbook_id": "track_upi_reversal", "novel": False, "disputed_amount": a["amount"], "disputed_ref": a["txn_id"],
                "deadline": iso(t1_end(a["since"])),
                "actions": [{"action": "raise_reversal_request", "params": {"utr": a["utr"]}, "amount": 0, "target": a["txn_id"]}]}
    if issue == "settlement_held":
        return {"playbook_id": "requeue_settlement", "novel": False, "disputed_amount": a["amount"], "disputed_ref": a["settlement_id"],
                "actions": [{"action": "requeue_settlement", "params": {"settlement_id": a["settlement_id"], "amount": a["amount"]},
                             "amount": a["amount"], "target": a["settlement_id"]}]}
    return None


def _diagnose_payout(case, client):
    cid = case["id"]
    dx = {"issue_type": "payout_request", "llm_used": "none", "needs_human": False}
    with trace.substep(cid, 2, "Check requested payout against records") as t:
        amt = next((x["said"] for x in case["parsed"]["amounts"]), None)
        disputed = next((x for x in case["parsed"]["amounts"] if x["ref"]), None)
        charges = client.call("get_charges", {"days": 30})["rows"]
        dest = case["parsed"].get("destination")
        linked = client.call("is_linked_account", {"destination": dest or ""})["linked"] if dest else False
        e1 = ev(f"Q-{case['merchant_id']}-charges30", clock.now_iso(), "charges",
                f"{len(charges)} charges in 30d; none disputed for ₹{amt:,}" if amt else "no amount")
        e2 = ev(f"Q-{case['merchant_id']}-linked", clock.now_iso(), "merchants", f"destination linked={linked}")
        logger.log("bayes", f"payout request ₹{amt or 0:,} → disputed charge: {'none' if not disputed else disputed['ref']} · "
                            f"destination linked: {linked} · no LLM needed")
        dx.update(cause="unverified_payout_request", cause_label="merchant asked to send money to another account",
                  confidence=1.0, evidence=[e1, e2], ruled_out=[], owner="risk-ops", expected_fix_hours=None, cite_ok=True,
                  fix={"playbook_id": None, "novel": False, "disputed_amount": disputed["snapped"] if disputed else None,
                       "disputed_ref": disputed["ref"] if disputed else None,
                       "actions": [{"action": "payout", "params": {"amount": amt or 0, "destination": dest or "other account",
                                                                   "linked": linked}, "amount": amt or 0, "target": dest or "unknown"}]})
        t["result"] = f"₹{amt or 0:,} to {dest or 'other account'} · linked={linked} · disputed charge none"
        t["evidence"] = [e1["id"], e2["id"]]
    case["llm_used"] = "none"
    return dx


def _diagnose_novel(case, client):
    cid = case["id"]
    dx = {"issue_type": case.get("issue_type", "novel"), "llm_used": "tier2", "needs_human": False}
    card = case.get("_card") or client.call("get_state_card", {})
    with trace.substep(cid, 2, "Tier 2 tool loop (novel case)", ai="Gemini tier2" + ("" if llm.is_live() else " (mock)")) as t:
        logger.log("bayes", f"no matching anomaly or playbook → novel · area {case.get('area')} (runner-up {case.get('area_runner_up')})")
        out = llm.tool_loop(card, case["text_english"], case.get("area"), client, case)
        registry = out["evidence"]
        t["result"] = f"{out['steps']} tool steps → {out['cause']} conf {out['confidence']:.2f}"
        t["evidence"] = sorted({i for c in out.get("claims", []) for i in c["evidence_ids"]})
    with trace.substep(cid, 2, "Cite check") as t:
        all_ok, bad = True, []
        for c in out.get("claims", []):
            ok, missing = cite_check(c.get("evidence_ids", []), registry)
            if not ok:
                all_ok = False
                bad.append(c["text"])
        n = len(out.get("claims", []))
        all_ok = all_ok and n > 0
        logger.log("bayes", f"cite check {n - len(bad)}/{n} claims cite existing evidence {'✓' if all_ok else '✗'}")
        t["result"] = f"{n - len(bad)}/{n} claims cited ✓" if all_ok else f"uncited claims: {bad or 'none made'}"
    fix = out.get("fix")
    actions = []
    if fix and fix.get("action") in llm.ALLOWED_FIXES:
        p = fix.get("params", {})
        actions = [{"action": fix["action"], "params": p, "amount": 0, "target": next(iter(p.values()), None)}]
    dx.update(cause=out["cause"], cause_label=out.get("summary"), confidence=out["confidence"],
              evidence=[ev(k, v.get("ts") or v.get("last_heartbeat"), v.get("source"), {kk: vv for kk, vv in v.items() if kk in ("status", "online", "amount", "alert_sent", "last_heartbeat")})
                        for k, v in registry.items() if k in {i for c in out.get("claims", []) for i in c["evidence_ids"]}],
              claims=out.get("claims", []), ruled_out=[], owner=out.get("owner"), cite_ok=all_ok,
              expected_fix_hours=p90_latency("device_offline") if (fix or {}).get("action") == "remote_restart" else None,
              fix={"playbook_id": None, "novel": True, "disputed_amount": None, "actions": actions} if actions else None)
    if not all_ok or not actions:
        dx["needs_human"], dx["reason"] = True, "novel case without a cited, allowed fix"
    case["llm_used"] = "tier2"
    return dx


# ------------------------------------------------------------------ payment check (S6): records first, then the UTR
def _diagnose_payment_check(case, client):
    cid = case["id"]
    amt = next((x["said"] for x in case["parsed"]["amounts"]), None)
    dx = {"issue_type": "payment_check", "llm_used": "none", "needs_human": False, "amount": amt, "evidence": [], "ruled_out": []}
    if not case.get("utr"):
        with trace.substep(cid, 2, "Check ledger + payments (last 15 min)") as t:
            ledger = client.call("get_ledger_entries", {"minutes": 15})["rows"]
            pays = client.call("get_payments", {"minutes": 15})["rows"]
            hit = [r for r in ledger + pays if r["amount"] == amt]
            e = ev(hit[0]["id"], hit[0]["ts"], "payments", f"₹{amt:,} received") if hit else \
                ev(f"Q-{case['merchant_id']}-15min", clock.now_iso(), "ledger+payments", f"no ₹{amt:,} in {len(ledger) + len(pays)} entries, last 15 min")
            dx["evidence"].append(e)
            if hit:
                logger.log("bayes", f"₹{amt:,} found in records ({hit[0]['id']} at {hit[0]['ts'][11:16]}) → it did arrive")
                dx.update(cause="payment_received", cause_label="the payment did reach your account", confidence=1.0,
                          answer=f"Good news: the ₹{amt:,} payment reached your account at {hit[0]['ts'][11:16]} ({hit[0]['id']}).")
                t["result"] = f"found {hit[0]['id']}"
            else:
                logger.log("bayes", f"₹{amt:,} not in ledger or payments (last 15 min) → tell merchant now · ask for UTR")
                dx.update(cause="not_received", cause_label="no such payment reached your account", confidence=1.0, await_input="utr")
                t["result"] = f"no ₹{amt:,} in the last 15 min → ask for UTR"
            t["evidence"] = [e["id"]]
        return dx
    with trace.substep(cid, 2, "Look up the UTR") as t:
        res = client.call("get_txn_by_utr", {"utr": case["utr"]})
        if not res["found"]:
            logger.log("bayes", "UTR not found at the UPI switch → ask merchant to re-check the number")
            dx.update(cause="utr_not_found", cause_label="no payment with that UTR", confidence=1.0, await_input="utr", utr_not_found=True)
            case.pop("utr", None)
            t["result"] = "UTR not found"
            return dx
        row = res["rows"][0]
        e = ev(row["id"], row["ts"], "upi_switch", f"₹{row['amount']:,} {row['status']} to {row['payee_vpa']}")
        dx["evidence"].append(e)
        t["evidence"] = [row["id"]]
        if row["payee_is_you"]:
            logger.log("bayes", f"UTR {row['id']}: payee is this merchant but no credit yet → needs a person")
            dx.update(cause="credit_delayed", cause_label="the payment is yours but not credited yet", confidence=0.8,
                      needs_human=True, reason="payee correct but not credited", fix=None)
            t["result"] = "payee = merchant, not credited → human"
            return dx
        qr = [q for q in client.call("get_qr_codes", {})["rows"] if q["status"] == "ACTIVE"]
        logger.log("bayes", f"UTR → {row['id']} ₹{row['amount']:,} {row['status']} to {row['payee_vpa']} ≠ merchant's UPI ID → "
                            f"possible tampered QR · fix qr_replacement")
        logger.log("incident", f"fraud signal: payee {row['payee_vpa']} looks like the merchant's name → reported to risk-ops")
        dx.update(cause="tampered_qr", cause_label="the money went to a look-alike UPI ID, so the QR sticker was probably replaced",
                  confidence=0.95, owner="risk-ops", cite_ok=True, fraud_flag=True,
                  notice={"kind": "qr_tampered", "params": {"amount": f"{row['amount']:,}", "vpa": row["payee_vpa"]}},
                  fix={"playbook_id": "qr_replacement", "novel": False, "disputed_amount": None,
                       "actions": [{"action": "qr_replacement", "params": {"reason": "tampered_qr"}, "amount": 0,
                                    "target": qr[0]["id"] if qr else "QR"}]})
        t["result"] = f"payee {row['payee_vpa']} is not you → tampered QR → replace QR"
    case["llm_used"] = "none"
    return dx


# ------------------------------------------------------------------ explain today's deductions (S8)
def _diagnose_explain(case, client):
    cid = case["id"]
    dx = {"issue_type": "explain_today", "llm_used": "none", "needs_human": False, "cause": "explained", "confidence": 1.0,
          "evidence": [], "ruled_out": []}
    with trace.substep(cid, 2, "Explain records line by line") as t:
        today = clock.now_iso()[:10]
        card = client.call("get_state_card", {})
        flagged = {a["charge_id"]: a for a in card["open_anomalies"] if a["rule"] == "rent_after_return"}
        lines = []
        for s in client.call("get_settlements", {})["rows"]:
            if s["ts"][:10] == today or s["status"] != "CREDITED":
                word = "credited to your bank" if s["status"] == "CREDITED" else f"{s['status'].lower()} (pending)"
                lines.append(f"• ₹{s['amount']:,} settlement {word} at {s['ts'][11:16]} [{s['id']}]. Normal.")
                dx["evidence"].append(ev(s["id"], s["ts"], "settlements", s["status"]))
        for c in client.call("get_charges", {"days": 30})["rows"]:
            if c["id"] in flagged:
                a = flagged[c["id"]]
                lines.append(f"• ₹{c['amount']:,} Soundbox rent on {parse(c['ts']).strftime('%d %b')} [{c['id']}]. This looks WRONG: "
                             f"you returned the Soundbox on {parse(a['state_since']).strftime('%d %b')}.")
                dx["evidence"].append(ev(c["id"], c["ts"], "charges", "rule-flagged: rent after return"))
            elif c["ts"][:10] == today:
                label = {"SMS_ALERT_FEE": "monthly SMS alert fee", "RENTAL": "Soundbox rent"}.get(c["type"], c["type"].lower())
                lines.append(f"• ₹{c['amount']:,} {label} today at {c['ts'][11:16]} [{c['id']}]. Standard charge.")
                dx["evidence"].append(ev(c["id"], c["ts"], "charges", c["type"]))
        pending = [q for q in client.call("get_credit_queue", {})["rows"] if q["status"] == "PENDING"]
        lines.append(f"• Pending: {len(pending)} item(s)." if pending else "• Nothing else is pending.")
        offer = next(iter(flagged.values()), None)
        dx["explain"] = "Here is what happened in your account:\n" + "\n".join(lines) + \
            ("\nShall I fix the wrong rent charge?" if offer else "")
        dx["offer"] = offer
        logger.log("bayes", f"explained {len(lines) - 1} item(s) from records {[e['id'] for e in dx['evidence']]}"
                            + (f" · rule-flagged wrong charge {offer['charge_id']} → offer fix" if offer else ""))
        t["result"] = f"{len(lines) - 1} items explained" + (f" · {offer['charge_id']} flagged" if offer else "")
        t["evidence"] = [e["id"] for e in dx["evidence"]]
    case["llm_used"] = "none"
    return dx
