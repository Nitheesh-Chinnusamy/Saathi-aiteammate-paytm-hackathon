"""M1: store/seed/clock/sim/bus/logger/MCP/context/anomaly rules."""
import json
import re

import pytest

from saathi.services import context_service
from saathi.core import event_bus
from saathi.core import logger
from saathi.mcp import mcp_server
from saathi.core import store
from saathi.core.clock import clock, hours_between
from saathi.mcp.mcp_client import MCPClient, MCPError


def test_seed_priors_and_p90():
    rows = store.q("SELECT cause, COUNT(*) n FROM historical_cases WHERE issue_type='rent_after_return' GROUP BY cause")
    total = sum(r["n"] for r in rows)
    pri = {r["cause"]: r["n"] / total for r in rows}
    assert pri["billing_event_missing"] == pytest.approx(0.70)
    assert pri["wrong_serial"] == pytest.approx(0.15)
    assert store.q1("SELECT COUNT(*) n FROM historical_cases")["n"] == 120


def test_clock_advance_fires_events_in_order():
    fired = []
    clock.schedule_in(7200, "b", lambda: fired.append(("b", clock.now_iso())))
    clock.schedule_in(3600, "a", lambda: fired.append(("a", clock.now_iso())))
    clock.advance(3 * 3600)
    assert [f[0] for f in fired] == ["a", "b"]
    assert 0.99 < hours_between(fired[0][1], fired[1][1]) < 1.01


def test_event_bus_wildcard():
    got = []
    event_bus.subscribe("sim.*", lambda t, p: got.append(t))
    event_bus.publish("sim.credit_posted", {})
    event_bus.publish("other.x", {})
    assert got == ["sim.credit_posted"]


def test_logger_since_and_scrub():
    a = logger.log("auth", "phone 9849012345 acct 31204455667788")
    lines = logger.since(a - 1)
    assert lines[-1]["id"] == a
    assert "9849012345" not in lines[-1]["msg"] and "98xxxxxx45" in lines[-1]["msg"]


def test_mcp_initialize_and_tools_list():
    c = MCPClient("M-2291")
    init = c.initialize()
    assert init["protocolVersion"] and init["serverInfo"]["name"]
    tools = {t["name"]: t for t in c.list_tools()}
    for name in ["get_state_card", "get_devices", "get_charges", "get_tickets", "get_billing_events", "get_warehouse_scan",
                 "get_settlements", "get_ledger_entries", "get_device_heartbeat", "get_payments",
                 "stop_rental", "post_credit", "requeue_settlement", "remote_restart"]:
        assert name in tools
        assert tools[name]["inputSchema"]["type"] == "object"
        assert "merchant_id" in tools[name]["inputSchema"]["required"]
    assert tools["get_devices"]["annotations"]["readOnlyHint"] is True
    assert tools["post_credit"]["annotations"]["readOnlyHint"] is False


def test_mcp_cross_merchant_rejected():
    c = MCPClient("M-2291")
    with pytest.raises(MCPError) as e:
        c.call("get_devices", {"merchant_id": "M-3107"})
    assert e.value.code == -32001
    with pytest.raises(MCPError) as e:   # row id belonging to someone else
        c.call("get_warehouse_scan", {"device_id": "SB-83"})
    assert e.value.code == -32001


def test_mcp_write_without_token_rejected():
    c = MCPClient("M-2291")
    with pytest.raises(MCPError) as e:
        c.call("post_credit", {"amount": 600, "ref": "C-5521", "gateway_token": "forged.token", "idempotency_key": "x"})
    assert e.value.code == -32003
    assert store.q1("SELECT COUNT(*) n FROM finance_queue")["n"] == 0


def test_mcp_raw_jsonrpc_errors():
    out = json.loads(mcp_server.handle_json('{"jsonrpc":"2.0","id":1,"method":"nope"}', {"merchant_id": "M-2291"}))
    assert out["error"]["code"] == -32601
    out = json.loads(mcp_server.handle_json('not json', {"merchant_id": "M-2291"}))
    assert out["error"]["code"] == -32700


def test_state_card_masks_pii_and_has_anomalies():
    card = context_service.get_state_card("M-2291")
    blob = json.dumps(card, ensure_ascii=False)
    assert "9849012345" not in blob and "31204455667788" not in blob
    assert card["merchant"]["phone"].startswith("98") and "x" in card["merchant"]["phone"]
    assert card["merchant"]["bank_account"].endswith("7788")
    assert [a["rule"] for a in card["open_anomalies"]] == ["rent_after_return"]
    a = card["open_anomalies"][0]
    assert a["charge_id"] == "C-5521" and a["device_model"] == "SB-4G"
    assert card["as_of"]


def test_anomalies_other_merchants():
    assert sorted(a["rule"] for a in context_service.get_state_card("M-4420")["open_anomalies"]) == ["incentive_pending", "settlement_held"]
    assert context_service.get_state_card("M-5150")["open_anomalies"] == []
    assert context_service.get_state_card("M-6001")["open_anomalies"] == []


def test_sim_payments_tick_on_advance():
    n0 = store.q1("SELECT COUNT(*) n FROM payments WHERE merchant_id='M-5150'")["n"]
    clock.advance(6 * 3600)
    assert store.q1("SELECT COUNT(*) n FROM payments WHERE merchant_id='M-5150'")["n"] == n0 + 3


def test_only_mcp_server_and_sim_query_merchant_tables():
    import glob
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    allowed = {"mcp_server.py", "paytm_sim.py", "seed.py", "store.py"}
    pat = re.compile(r"(FROM|INTO|UPDATE)\s+(merchants|devices|charges|billing_events|warehouse_scans|tickets|settlements|payouts|ledger_entries|finance_queue|payments|heartbeats)\b", re.I)
    offenders = []
    for f in glob.glob(os.path.join(root, "app.py")) + glob.glob(os.path.join(root, "saathi", "**", "*.py"), recursive=True):
        if os.path.basename(f) in allowed:
            continue
        if pat.search(open(f, encoding="utf-8").read()):
            offenders.append(os.path.basename(f))
    assert offenders == []
