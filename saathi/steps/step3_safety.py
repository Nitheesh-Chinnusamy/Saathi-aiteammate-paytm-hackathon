"""step3_safety.py - Step 3 Safety check: policy rules judge the EXACT proposed change.

gateway dry-run -> diff -> named policy rules (each pass/fail with a reason) -> decision
ALLOW | NEEDS_APPROVAL | DENY. On ALLOW (or human approval) issue an HMAC-signed scoped token
{merchant_id, case_id, actions, max_amount, expires (10 min virtual)}.

Production: OPA/Rego policy engine evaluating the dry-run diff; tokens from a KMS-backed signer.
"""
import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import timedelta

from saathi.services import human_desk
from saathi.core import logger
from saathi.sim import seed
from saathi.core import store
from saathi.core import trace
from saathi.core.clock import clock, iso, parse

DENY, APPROVAL = "DENY", "NEEDS_APPROVAL"


# ------------------------------------------------------------------ token
def _secret():
    return (os.environ.get("SECRET_KEY") or "saathi-dev-secret").encode()


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload):
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def decode(token):
    return json.loads(_unb64(token.rsplit(".", 1)[0]))


def issue_token(merchant_id, case_id, actions, max_amount, ttl_min=None):
    ttl = ttl_min or seed.policy("token_ttl_min") or 10
    tools = sorted({{"goodwill_credit": "post_credit", "rbi_compensation": "post_customer_credit"}.get(a, a) for a in actions})
    payload = {"merchant_id": merchant_id, "case_id": case_id, "actions": tools, "max_amount": int(max_amount),
               "iat": clock.now_iso(), "expires": iso(clock.now() + timedelta(minutes=ttl)), "jti": uuid.uuid4().hex[:12]}
    tok = sign(payload)
    logger.log("token", f"scope {{{merchant_id}, {'+'.join(tools)}, ≤₹{int(max_amount):,}, {ttl}m}} · HMAC-SHA256")
    return tok


def verify_token(token, merchant_id, action, amount=0):
    if not token or "." not in token:
        return False, "missing or malformed token"
    body, sig = token.rsplit(".", 1)
    good = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False, "bad signature"
    try:
        p = json.loads(_unb64(body))
    except Exception:
        return False, "unreadable payload"
    if p.get("merchant_id") != merchant_id:
        return False, f"token for {p.get('merchant_id')}, not {merchant_id}"
    if action not in p.get("actions", []):
        return False, f"action {action} not in scope"
    if (amount or 0) > p.get("max_amount", 0):
        return False, f"amount ₹{amount} > token max ₹{p.get('max_amount')}"
    if clock.now() >= parse(p["expires"]):
        return False, "token expired"
    return True, "ok"


# ------------------------------------------------------------------ policy rules
def evaluate(merchant, dx, dry):
    """Returns list of {name, passed, severity, reason}. Pure function over the diff + diagnosis."""
    cap = seed.policy("auto_credit_cap")
    max_cr = seed.policy("max_credits_30d")
    fix = dx.get("fix") or {}
    diff = dry["diff"]
    credits = [d for d in diff if d["action"] in ("post_credit", "goodwill_credit", "payout", "rbi_compensation")]
    money_out = sum(d["amount"] for d in credits)
    rules = []

    def rule(name, passed, severity, ok_reason, fail_reason):
        rules.append({"name": name, "passed": bool(passed), "severity": severity, "reason": ok_reason if passed else fail_reason})

    not_owned = [d["target"] for d in diff if not d["owned"] and d["action"] != "payout"]
    rule("target_belongs_to_merchant", not not_owned, DENY, "every target is the logged-in merchant's",
         f"{not_owned} not owned by {merchant['id']}")
    external = [d for d in diff if d["action"] == "payout" and not d.get("linked")]
    rule("no_payout_to_unlinked_account", not external, DENY, "no money to unlinked accounts",
         f"₹{sum(d['amount'] for d in external):,} to unlinked destination {external[0]['target'] if external else ''}")
    disputed = fix.get("disputed_amount")
    over = []
    for d in credits:
        limit = (seed.policy("goodwill_amount") if d["action"] == "goodwill_credit"
                 else dx.get("comp_limit") if d["action"] == "rbi_compensation" else disputed)
        if limit is None or d["amount"] > limit:
            over.append((d, limit))
    rule("credit_not_above_disputed_charge", not over, DENY,
         "credit ≤ disputed amount" + (f" ₹{disputed:,}" if disputed else ""),
         "; ".join(f"₹{d['amount']:,} > disputed {'₹%s' % f'{l:,}' if l else 'none (no disputed charge)'}" for d, l in over))
    rule("fraud_flag_clear", not merchant.get("fraud_flag"), DENY, "no fraud flag", "merchant carries a fraud flag")
    rule("auto_credit_cap", money_out <= cap, APPROVAL, f"₹{money_out:,} ≤ auto cap ₹{cap:,}", f"₹{money_out:,} > auto cap ₹{cap:,}")
    n_new = len([d for d in credits if d["action"] not in ("payout", "rbi_compensation")])   # regulatory compensation is exempt
    rule("max_credits_per_30d", dry["credits_30d"] + n_new <= max_cr, APPROVAL,
         f"{dry['credits_30d']}+{n_new} ≤ {max_cr} credits/30d", f"{dry['credits_30d']}+{n_new} > {max_cr} credits/30d")
    pb = store.q1("SELECT * FROM playbooks WHERE id=?", (fix.get("playbook_id"),)) if fix.get("playbook_id") else None
    if dx.get("goodwill"):
        rule("playbook_autonomy", True, APPROVAL, "goodwill per compensation policy", "")
    elif dx.get("regulatory"):
        rule("playbook_autonomy", True, APPROVAL, "regulatory compensation (RBI TAT ₹100/day)", "")
    elif fix.get("novel") or not pb:
        rule("playbook_autonomy", False, APPROVAL, "", "novel case: no verified playbook → human approval")
    elif pb["autonomy"] == "ACT_ALONE":
        rule("playbook_autonomy", True, APPROVAL, f"{pb['id']} = ACT_ALONE", "")
    else:
        amt = max([d["amount"] for d in diff] + [0])
        lim = pb["approval_above"] or 0
        rule("playbook_autonomy", amt <= lim, APPROVAL, f"{pb['id']} ACT_WITH_APPROVAL, ₹{amt:,} ≤ ₹{lim:,}",
             f"{pb['id']} = ACT_WITH_APPROVAL above ₹{lim:,} (₹{amt:,})")
    return rules


def decide(rules):
    if any(not r["passed"] and r["severity"] == DENY for r in rules):
        return "DENY"
    if any(not r["passed"] for r in rules):
        return "NEEDS_APPROVAL"
    return "ALLOW"


def safety_check(case, dx):
    from saathi.steps import step4_act
    cid = case["id"]
    out = {}
    with trace.substep(cid, 3, "Gateway dry-run diff") as t:
        dry = step4_act.dry_run(case["merchant_id"], dx["fix"]["actions"])
        out["diff"] = dry["diff"]
        t["result"] = " · ".join(f"{d['target']}: {d['before']} → {d['after']}" for d in dry["diff"])
        t["evidence"] = [d["target"] for d in dry["diff"]]
    with trace.substep(cid, 3, "Policy rules") as t:
        rules = evaluate(case["merchant"], dx, dry)
        decision = decide(rules)
        n_ok = sum(r["passed"] for r in rules)
        fails = [r for r in rules if not r["passed"]]
        logger.log("policy", f"{n_ok}/{len(rules)} rules ✓ → {decision}" + (" · " + " · ".join(f"✗ {r['name']}" for r in fails) if fails else ""))
        for r in fails:
            logger.log("policy", f"✗ {r['name']}: {r['reason']}")
        out.update(rules=rules, decision=decision)
        t["result"] = f"{n_ok}/{len(rules)} ✓ → {decision}"
    with trace.substep(cid, 3, "Decision") as t:
        if decision == "ALLOW":
            out["token"] = issue_token(case["merchant_id"], cid, [a["action"] for a in dx["fix"]["actions"]],
                                       max(a["amount"] for a in dx["fix"]["actions"]))
            t["result"] = "ALLOW · scoped token issued"
        elif decision == "NEEDS_APPROVAL":
            out["desk_id"] = human_desk.open_item(cid, case["merchant_id"], "approval",
                                                  f"Approve {'+'.join(a['action'] for a in dx['fix']['actions'])} for {case['merchant_id']}",
                                                  {"diagnosis": _dx_summary(dx), "diff": dry["diff"], "rules": rules})
            t["result"] = f"NEEDS_APPROVAL → desk {out['desk_id']}"
        else:
            out["desk_id"] = human_desk.open_item(cid, case["merchant_id"], "blocked",
                                                  f"Blocked request flagged for review ({case['merchant_id']})",
                                                  {"diagnosis": _dx_summary(dx), "diff": dry["diff"], "rules": rules,
                                                   "identity_claims_ignored": case.get("identity_claims_ignored")})
            t["result"] = f"DENY → flagged to desk {out['desk_id']}"
    return out


def _dx_summary(dx):
    return {"cause": dx.get("cause"), "label": dx.get("cause_label"), "confidence": dx.get("confidence"),
            "llm_used": dx.get("llm_used"), "evidence": [e["id"] for e in dx.get("evidence", [])],
            "claims": dx.get("claims"), "owner": dx.get("owner")}
