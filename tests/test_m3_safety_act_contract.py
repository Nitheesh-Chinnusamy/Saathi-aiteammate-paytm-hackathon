"""M3: Step 3 Safety, Step 4 Act (gateway), Step 5 Contract."""
import pytest

from saathi.steps import step1_understand as s1
from saathi.steps import step2_diagnose as s2
from saathi.steps import step3_safety as s3
from saathi.steps import step4_act as s4
from saathi.steps import step5_contract as s5
from saathi.core import store
from saathi.core.clock import clock
from saathi.mcp.mcp_client import MCPClient, MCPError
from saathi.sim.seed import SCENARIOS


def prep(sid, choice=None):
    sc = SCENARIOS[sid]
    c = s1.understand(sc["merchant_id"], text=sc["text"], scenario_id=sid)
    if choice:
        c = s1.understand(sc["merchant_id"], choice=choice, case=c)
    return c, s2.diagnose(c)


def rules_by_name(res):
    return {r["name"]: r for r in res["rules"]}


def test_s1_allow_all_rules_pass():
    c, d = prep("S1")
    res = s3.safety_check(c, d)
    assert res["decision"] == "ALLOW"
    assert all(r["passed"] for r in res["rules"])
    p = s3.decode(res["token"])
    assert p["merchant_id"] == "M-2291" and p["max_amount"] == 600
    assert p["actions"] == ["post_credit", "stop_rental"]


def test_s3_needs_approval_above_threshold():
    c, d = prep("S3", choice="A-SH-S-9012")
    res = s3.safety_check(c, d)
    assert res["decision"] == "NEEDS_APPROVAL"
    assert not rules_by_name(res)["playbook_autonomy"]["passed"]
    assert "token" not in res and res["desk_id"]


def test_s4_novel_needs_approval():
    c, d = prep("S4")
    res = s3.safety_check(c, d)
    assert res["decision"] == "NEEDS_APPROVAL"
    assert "novel" in rules_by_name(res)["playbook_autonomy"]["reason"]


def test_s5_deny_unlinked_and_no_disputed():
    c, d = prep("S5")
    res = s3.safety_check(c, d)
    r = rules_by_name(res)
    assert res["decision"] == "DENY"
    assert not r["no_payout_to_unlinked_account"]["passed"]
    assert not r["credit_not_above_disputed_charge"]["passed"]
    assert "no disputed" in r["credit_not_above_disputed_charge"]["reason"]
    assert store.q1("SELECT kind FROM desk WHERE id=?", (res["desk_id"],))["kind"] == "blocked"


def test_policy_cap_and_ownership_units():
    merchant = {"id": "M-2291", "fraud_flag": 0}
    dx = {"fix": {"playbook_id": "stop_rental_and_credit", "disputed_amount": 1500, "actions": []}}
    dry = {"credits_30d": 0, "diff": [{"action": "post_credit", "target": "C-1", "owned": True, "amount": 1500}]}
    r = {x["name"]: x for x in s3.evaluate(merchant, dx, dry)}
    assert not r["auto_credit_cap"]["passed"] and s3.decide(list(r.values())) == "NEEDS_APPROVAL"
    dry["diff"][0]["owned"] = False
    assert s3.decide(s3.evaluate(merchant, dx, dry)) == "DENY"
    dry = {"credits_30d": 2, "diff": [{"action": "post_credit", "target": "C-1", "owned": True, "amount": 100}]}
    r = {x["name"]: x for x in s3.evaluate(merchant, dx, dry)}
    assert not r["max_credits_per_30d"]["passed"]
    assert s3.decide(s3.evaluate({"id": "M-1", "fraud_flag": 1}, dx, dry)) == "DENY"


def test_token_rejections():
    tok = s3.issue_token("M-2291", "C-1", ["post_credit"], 600)
    assert s3.verify_token(tok, "M-2291", "post_credit", 600) == (True, "ok")
    assert not s3.verify_token(tok, "M-3107", "post_credit", 600)[0]          # wrong merchant
    assert not s3.verify_token(tok, "M-2291", "post_credit", 601)[0]          # amount above scope
    assert not s3.verify_token(tok, "M-2291", "stop_rental", 0)[0]            # action not in scope
    body, sig = tok.rsplit(".", 1)
    assert not s3.verify_token(body + "." + "0" * len(sig), "M-2291", "post_credit", 1)[0]   # forged
    clock.advance(11 * 60)
    ok, reason = s3.verify_token(tok, "M-2291", "post_credit", 600)
    assert not ok and reason == "token expired"


def test_gateway_rejects_bad_token_and_mcp_enforces_too():
    with pytest.raises(s4.GatewayError):
        s4.execute("C-1", "M-2291", {"action": "post_credit", "params": {"amount": 600, "ref": "C-5521", "kind": "refund"},
                                     "amount": 600}, s3.issue_token("M-3107", "C-1", ["post_credit"], 600))
    tok = s3.issue_token("M-2291", "C-1", ["post_credit"], 100)
    with pytest.raises(MCPError):   # even bypassing the gateway, the server checks the amount scope
        MCPClient("M-2291").call("post_credit", {"amount": 600, "ref": "C-5521", "gateway_token": tok, "idempotency_key": "x"})


def test_idempotency_prevents_double_credit():
    tok = s3.issue_token("M-3107", "C-77", ["post_credit"], 600)
    a = {"action": "post_credit", "params": {"amount": 600, "ref": "C-6610", "kind": "refund"}, "amount": 600}
    r1 = s4.execute("C-77", "M-3107", a, tok)
    r2 = s4.execute("C-77", "M-3107", a, tok)
    assert r1["replay"] is False and r2["replay"] is True
    assert store.q1("SELECT COUNT(*) n FROM finance_queue WHERE merchant_id='M-3107'")["n"] == 1
    assert store.q1("SELECT COUNT(*) n FROM compensations WHERE case_id='C-77'")["n"] == 1


def test_compensation_registered_before_execute():
    c, d = prep("S1")
    res = s3.safety_check(c, d)
    out = s4.act(c, d, res["token"])
    comps = store.q("SELECT action, compensation FROM compensations WHERE case_id=? ORDER BY id", (c["id"],))
    assert [x["compensation"] for x in comps] == ["resume_rental", "reverse_credit"]
    assert len(out) == 2 and all(o["audit_seq"] for o in out)


def test_audit_chain_detects_tampering():
    for i in range(3):
        s4.audit_append(f"C-{i}", "post_credit", {"amount": 100 + i})
    assert s4.verify_chain()["ok"]
    store.ex("UPDATE audit_log SET row_json=? WHERE seq=2", ('{"amount": 999999}',))
    v = s4.verify_chain()
    assert not v["ok"] and v["broken_at"] == 2


def test_contract_deadline_from_p90():
    assert s5.deadline_hours(36) == 48
    assert s5.deadline_hours(18) == 24
    assert s5.deadline_hours(2) == 3
    c, d = prep("S1")
    res = s3.safety_check(c, d)
    ex = s4.act(c, d, res["token"])
    k = s5.create(c, d, ex)
    assert k["hours"] == 48 and k["p90"] == 36
    assert [x["name"] for x in k["checks"]] == ["rental_stopped", "credit_posted"]
    assert k["message"]["lang"] == "te" and "600" in k["message"]["text"] and "48" in k["message"]["text"]
