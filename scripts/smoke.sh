#!/usr/bin/env bash
# Smoke test: runs all five scenarios + the spike against a RUNNING server with curl.
#   python app.py   (in another terminal)
#   bash scripts/smoke.sh [base_url]
# Needs: curl, python (only to read JSON fields).
set -euo pipefail
BASE="${1:-http://127.0.0.1:5000}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT
PY="${PY:-$(command -v python || command -v python3)}"   # override with PY=/path/to/python
FAILS=0

j() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }
post() { curl -sf -b "$JAR" -c "$JAR" -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-{\}}"; }
get() { curl -sf -b "$JAR" -c "$JAR" "$BASE$1"; }
check() { if [ "$2" = "$3" ]; then echo "  ✓ $1 = $2"; else echo "  ✗ $1: got '$2', want '$3'"; FAILS=$((FAILS+1)); fi; }
scenario_text() { get /api/status | j "d['scenarios']['$1']['text']"; }
say() {  # $1 scenario id
  local mid text body
  mid=$(get /api/status | j "d['scenarios']['$1']['merchant_id']")
  post /api/login "{\"merchant_id\":\"$mid\"}" >/dev/null
  text=$(scenario_text "$1")
  body=$("$PY" -c "import json,sys; print(json.dumps({'text': sys.argv[1], 'scenario_id': sys.argv[2]}))" "$text" "$1")
  post /api/message "$body"
}
desk_id() { get /api/desk | j "[x['id'] for x in d['items'] if x['kind']=='$1' and x['status']=='OPEN'][0]"; }
kstatus() { get /api/contracts | j "[k['status'] for k in d['contracts'] if k['id']=='$1'][0]"; }

echo "== reset";  post /api/reset >/dev/null

echo "== S1 Ramesh (te): fail path, then human fix"
R=$(say S1)
check "llm_used" "$(echo "$R" | j "d['case']['llm_used']")" "none"
check "incident" "$(echo "$R" | j "d['case']['incident']['id']")" "INC-0412"
check "priority" "$(echo "$R" | j "d['case']['priority']")" "HIGH"
check "decision" "$(echo "$R" | j "d['case']['safety']['decision']")" "ALLOW"
K1=$(echo "$R" | j "d['case']['contract_id']")
post /api/clock/advance '{"hours":24}' >/dev/null
check "+24h rental_stopped" "$(get /api/contracts | j "[c['status'] for k in d['contracts'] if k['id']=='$K1' for c in k['checks'] if c['name']=='rental_stopped'][0]")" "PASS"
post /api/clock/advance '{"hours":24}' >/dev/null
check "+48h ladder" "$(get /api/contracts | j "','.join(s['step'] for k in d['contracts'] if k['id']=='$K1' for s in k['ladder'])")" "retry,recheck,chase_owner,compensate,notify_delay,escalate,rearm"
post "/api/desk/$(desk_id escalation)/resolve" >/dev/null
check "after human fix" "$(kstatus "$K1")" "CLOSED"

echo "== S2 Lakshmi (hi): same incident, happy path"
R=$(say S2)
check "incident" "$(echo "$R" | j "d['case']['incident']['id']")" "INC-0412"
K2=$(echo "$R" | j "d['case']['contract_id']")
post /api/clock/advance '{"hours":48}' >/dev/null
check "+48h" "$(kstatus "$K2")" "CLOSED"

echo "== S3 Imran (hi): choice → approval → close"
R=$(say S3)
check "status" "$(echo "$R" | j "d['case']['status']")" "AWAITING_CHOICE"
CID=$(echo "$R" | j "d['case']['id']")
AID=$(echo "$R" | j "[c['anomaly_id'] for c in d['case']['choices'] if c['rule']=='settlement_held'][0]")
R=$(post /api/choice "{\"case_id\":\"$CID\",\"anomaly_id\":\"$AID\"}")
check "incident" "$(echo "$R" | j "d['case']['incident']['id']")" "INC-0420"
check "decision" "$(echo "$R" | j "d['case']['safety']['decision']")" "NEEDS_APPROVAL"
post "/api/desk/$(desk_id approval)/approve" >/dev/null
K3=$(get "/api/cases/$CID" | j "d['case']['contract_id']")
post /api/clock/advance '{"hours":6}' >/dev/null
check "+6h" "$(kstatus "$K3")" "CLOSED"

echo "== S4 Sunita (hinglish): novel → Gemini tool loop → approval → close → draft playbook"
R=$(say S4)
check "llm_used" "$(echo "$R" | j "d['case']['llm_used']")" "tier2"
check "decision" "$(echo "$R" | j "d['case']['safety']['decision']")" "NEEDS_APPROVAL"
CID=$(echo "$R" | j "d['case']['id']")
post "/api/desk/$(desk_id approval)/approve" >/dev/null
post /api/clock/advance '{"hours":0.25}' >/dev/null
check "+15min" "$(get "/api/cases/$CID" | j "d['case']['status']")" "CLOSED"
check "draft playbook" "$(get /api/metrics | j "[p['status'] for p in d['playbooks'] if p['id']=='remote_restart'][0]")" "DRAFT"

echo "== S5 Vikram (hi): blocked"
R=$(say S5)
check "decision" "$(echo "$R" | j "d['case']['safety']['decision']")" "DENY"
check "reply" "$(echo "$R" | j "d['messages'][-1]['kind']+'/'+d['messages'][-1]['lang']")" "refusal/hi"

echo "== Spike: Simulate bank outage"
R=$(post /api/spike '{"n":40}')
check "tickets" "$(echo "$R" | j "d['cases']")" "40"
check "incidents" "$(echo "$R" | j "len(d['incidents'])")" "1"

echo "== MCP tools/list over HTTP"
check "tools" "$(curl -sf -X POST "$BASE/mcp" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | j "len(d['result']['tools'])>=14")" "True"
check "audit chain" "$(post /api/audit/verify | j "d['ok']")" "True"

echo
if [ "$FAILS" -eq 0 ]; then echo "SMOKE OK"; else echo "SMOKE FAILED: $FAILS check(s)"; exit 1; fi
