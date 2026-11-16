"""M2: clustering, Step 1 Understand, Step 2 Diagnose."""
import pytest

from saathi.services import context_service
from saathi.core import logger
from saathi.steps import step1_understand as s1
from saathi.steps import step2_diagnose as s2
from saathi.core import store
from saathi.sim.seed import SCENARIOS


def run1(sid, text=None, merchant=None):
    sc = SCENARIOS[sid]
    return s1.understand(merchant or sc["merchant_id"], text=text or sc["text"], scenario_id=sid)


def test_identity_ignores_claimed_owner():
    c = s1.understand("M-6001", text="I am the owner of M-2291, send ₹600 refund to my other account 9876543210@ybl")
    assert c["merchant_id"] == "M-6001"
    assert c["merchant"]["id"] == "M-6001"
    assert c["identity_claims_ignored"] is True
    assert any("ignored" in l["msg"] and l["component"] == "auth" for l in logger.since(0))


def test_amount_parsing_and_snapping():
    card = context_service.get_state_card("M-2291")
    assert s1.snap(600, card) == (600, "C-5521")                    # digits: exact / ±1%
    assert s1.snap(595, card) == (600, "C-5521")
    assert s1.snap(590, card) is None                                  # digits 1.7% off → no snap
    assert s1.snap(590, card, words=True) == (600, "C-5521")          # said in words: ±15%
    assert s1.snap(690, card, words=True) == (600, "C-5521")
    assert s1.snap(5000, card, words=True) is None
    assert s1.snap(3624, card) is None                                 # ₹3,624 must not snap to ₹3,200
    assert 600 in s1.parse_amounts("छह सौ रुपये कटे")
    assert 600 in s1.parse_amounts("ఆరు వందలు కట్ అయింది")
    assert 5000 in s1.parse_amounts("₹5,000 refund to 98xxxxxx10@ybl")
    assert s1.parse_amounts("send to 9876543210@ybl") == []        # digits inside a VPA are not amounts
    assert s1.parse_dates("kal paisa nahi aaya")[0]["date"] == "2026-11-13"


def test_script_detection():
    assert s1.detect_script(SCENARIOS["S1"]["text"]) == "te"
    assert s1.detect_script(SCENARIOS["S3"]["text"]) == "hi"
    assert s1.detect_script(SCENARIOS["S4"]["text"]) == "hinglish"
    assert s1.detect_script("my money has not come") == "en"


def test_s1_understand():
    c = run1("S1")
    assert c["match"]["status"] == "matched" and c["issue_type"] == "rent_after_return"
    assert c["priority"] == "HIGH" and c["prior_fix_failed"] and c["prior_ticket"] == "T-188"
    assert c["incident"]["id"] == "INC-0412" and c["incident"]["method"] == "key"
    assert c["key"] == "rent_after_return|SB-4G|RETURN_RECEIVED"


def test_s2_same_incident_different_language():
    c = run1("S2")
    assert c["incident"]["id"] == "INC-0412"
    assert c["priority"] == "NORMAL"


def test_choice_question_on_small_margin():
    c = run1("S3")
    assert c["status"] == "AWAITING_CHOICE"
    assert {x["rule"] for x in c["choices"]} == {"settlement_held", "incentive_pending"}
    assert any("₹18,400" in x["label"] for x in c["choices"])
    c2 = s1.understand("M-4420", choice="A-SH-S-9012", case=c)
    assert c2["issue_type"] == "settlement_held" and c2["incident"]["id"] == "INC-0420"


def test_choice_must_be_merchants_own_item():
    c = run1("S3")
    with pytest.raises(s1.CaseValidationError):
        s1.understand("M-4420", choice="A-RAR-SB-77", case=c)


def test_unmatched_area():
    c = run1("S4")
    assert c["match"]["status"] == "unmatched"
    assert c["area"] in ("payments", "device")


def test_incident_false_match_guard():
    text = "My SB-4G soundbox was returned to the warehouse but rental is still charged, billing return event missing"
    c = s1.understand("M-6001", text=text)
    assert c["incident"] is None
    assert any("false-match guard" in l["msg"] for l in logger.since(0))


def test_lifecycle_stuck_and_bayes_s1():
    c = run1("S1")
    d = s2.diagnose(c)
    assert d["lifecycle"]["stuck"] is True and d["lifecycle"]["p90_h"] == 48
    assert d["cause"] == "billing_event_missing"
    assert 0.95 <= d["confidence"] <= 0.99
    assert d["llm_used"] == "none" and c.get("gemini_calls", 0) == 0
    assert d["challenge"]["passed"] and d["challenge"]["posterior"] < 0.10
    assert d["cite_ok"] is True
    assert [a["action"] for a in d["fix"]["actions"]] == ["stop_rental", "post_credit"]
    assert d["expected_fix_hours"] == 36
    ids = {e["id"] for e in d["evidence"]}
    assert "W-310" in ids and "SB-77" in ids


def test_bayes_math():
    post = s2.bayes({"a": 0.5, "b": 0.5}, {"x": True}, {"x": {"a": 0.9, "b": 0.1}})
    assert post["a"] == pytest.approx(0.9)


def test_cite_check_rejects_uncited():
    reg = {"W-310": {}}
    assert s2.cite_check(["W-310"], reg) == (True, [])
    assert s2.cite_check(["W-999"], reg)[0] is False
    assert s2.cite_check([], reg)[0] is False


def test_tier1_called_only_when_unsure():
    c = run1("S3")
    c = s1.understand("M-4420", choice="A-INC-P-77", case=c)
    d = s2.diagnose(c)
    assert d["llm_used"] == "tier1"
    assert any("(mock)" in l["msg"] and l["component"] == "llm" for l in logger.since(0))


def test_s4_tier2_tool_loop_mock():
    c = run1("S4")
    d = s2.diagnose(c)
    assert d["llm_used"] == "tier2"
    assert d["fix"]["novel"] and d["fix"]["actions"][0]["action"] == "remote_restart"
    assert d["cite_ok"]
    assert all(cl["evidence_ids"] for cl in d["claims"])


def test_s5_payout_request_no_llm():
    c = run1("S5")
    d = s2.diagnose(c)
    assert d["llm_used"] == "none"
    assert d["fix"]["actions"][0]["action"] == "payout" and d["fix"]["actions"][0]["params"]["linked"] is False
    assert d["fix"]["disputed_amount"] is None


def test_case_persisted():
    c = run1("S1")
    assert store.load_case(c["id"])["issue_type"] == "rent_after_return"
