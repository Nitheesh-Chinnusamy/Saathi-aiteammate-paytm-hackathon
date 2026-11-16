# Saathi: an AI support teammate for Paytm merchants that proves its fixes

Hackathon prototype for the **Paytm Build for India AI Hackathon, Track 3: Autonomous AI Teammates**.

Most support bots mark a ticket "resolved" once the customer stops replying. **Saathi closes a ticket only after a check on the system of record passes.** Each case goes through seven visible steps:

1. **Understand.** Identity comes from the login, never from the message. Saathi reads a masked state card through MCP, transcribes voice with Sarvam, parses amounts and dates, matches the complaint to the merchant's own open items, checks for broken promises and attaches an incident.
2. **Diagnose.** Cheap data tests and a Bayes update find the cause. Gemini is called only when confidence stays below 0.85 (Tier 1) or the case is novel (Tier 2 tool loop).
3. **Safety check.** Named policy rules judge the exact dry-run diff and return ALLOW, NEEDS_APPROVAL or DENY. ALLOW issues a scoped HMAC token.
4. **Act.** Every change goes through one gateway with token checks, idempotency, a compensation registered before execution, and a hash-chained audit log.
5. **Contract.** Saathi records a checkable promise. The deadline is the P90 of historical latency plus a buffer.
6. **Verify.** Event-driven predicate checks run against the simulated Paytm systems (not the gateway's reply), backed by a deadline timer and `reconcile()`.
7. **If the check fails.** A ladder runs in order: retry, re-check, chase the owner, compensate, send a delay message, escalate to a human, re-arm the deadline.

A **learning loop** wraps all seven steps. Verified outcomes update the priors and P90 latencies. Novel fixes become draft playbooks. Spike detection turns 40 tickets into one incident.

---

## Run it

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt && python app.py     # http://127.0.0.1:5000  (threaded=True)
python -m pytest -q                                  # 73 tests: unit, MCP, logs, end to end
bash scripts/smoke.sh                                # all five scenarios + the spike, run against the live server with curl
```

Built and tested on Python 3.13; the code also runs on 3.10 or newer. **No API keys are needed:** everything runs in mock mode by default.
The architecture page is at `/architecture`. It lists the 7 steps with their prototype module and production equivalent, and fetches the MCP tool list live from `tools/list`.

Public URL: the `Procfile` runs gunicorn (`--workers 1 --threads 8`) on Render, Railway or Heroku. Use a single worker because the world state (clock, logs, SQLite) lives in memory.

### Project layout

```
app.py                  Flask app: UI pages, JSON API, POST /mcp (entry point; Procfile runs app:app)
saathi/
  core/                 clock (virtual time), event_bus, logger, store (SQLite), trace
  steps/                step1_understand ... step7_remediate: the 7 visible steps
  mcp/                  mcp_server (JSON-RPC tools, gated writes), mcp_client
  integrations/         llm (Gemini), sarvam (speech-to-text, text-to-speech); mock mode without keys
  sim/                  paytm_sim (simulated Paytm back-end), seed (demo data, templates), languages_extra, anomaly_rules
  services/             pipeline (wires the steps), conversation (chat layer), notify, context_service,
                        human_desk, learning, clustering
templates/              index.html (demo UI), cases.html (/cases), architecture.html (/architecture)
static/                 app.js, user.css, style.css
tests/                  pytest suites (mock mode, no keys needed)
scripts/smoke.sh        curl smoke test against a running server
TESTING.md              step-by-step manual test of the demo (two users, four problems)
```

The demo UI offers two users: **Merchant** (Ramesh) and **Paytm customer** (Kiran), each followed by a language choice.
The four everyday problems it walks through are described on `/cases`.

### Environment variables (`.env`, see `.env.example`)

| var | default | effect |
|---|---|---|
| `SARVAM_API_KEY` | empty → mock | Live Saaras v3 speech-to-text. Each clip gets two calls: `mode=codemix` gives the original text, `mode=translate` gives the English copy. |
| `GEMINI_API_KEY` | empty → mock | Live Gemini through the OpenAI-compatible endpoint, with `reasoning_effort: "low"`. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model name sent to Gemini. |
| `MCP_TRANSPORT` | `inprocess` | `http` sends the same JSON-RPC bytes to `POST /mcp` (set `MCP_BASE_URL` if the port is not 5000). |
| `SECRET_KEY` | dev value | HMAC key for gateway tokens, MCP session tokens and the Flask session. |

The top bar shows **Sarvam: live/mock** and **Gemini: live/mock** badges. Every mocked call writes a log line ending in `(mock)`.

---

## What is live vs mock

| Piece | Mock (default) | Live |
|---|---|---|
| Speech-to-text (Step 1) | A recorded clip maps to the scenario's seeded transcript. Logged as `(mock)` with the real elapsed ms. | Sarvam Saaras v3 `codemix` and `translate`. The browser records WebM with MediaRecorder; clips are under 30 s. |
| Tier 1 `score_options` | Picks the hypothesis with the highest posterior, cites the evidence, and validates the result as JSON like a real reply. | Gemini returns strict JSON `{choice, confidence, evidence_ids}`, which is validated. |
| Tier 2 `tool_loop` | A deterministic planner calls the **real** read-only MCP tools (devices → heartbeat → payments) and draws its conclusion from the returned rows. | Gemini with OpenAI-style `tools`: read-only MCP tools only, at most 6 steps, and every claim cite-checked. |
| Typed-text translation | Seeded scenario texts use their seeded English copy. Other text passes through unchanged. | Gemini translate. |
| Paytm systems | `saathi/sim/paytm_sim.py`, which logs every line with the `[sim]` tag. | Production would use Paytm's billing, ledger, settlement and device services. |

**Honest logs rule.** Every log line comes from the code path that is running at that moment; no narrative line is hard-coded. When bulk work (the 40-ticket outage) mutes its per-call lines, it writes one computed summary line instead.

---

## Architecture → files

| Block | File | Production equivalent |
|---|---|---|
| Web app + UI + `POST /mcp` | `app.py`, `templates/`, `static/` | API gateway + agent service; an ops console |
| Orchestration | `saathi/services/pipeline.py` *(added: wires the steps and the subscribers)* | Temporal workflow per case |
| Step 1 Understand | `saathi/steps/step1_understand.py`, `saathi/services/context_service.py`, `saathi/sim/anomaly_rules.py`, `saathi/services/clustering.py`, `saathi/integrations/sarvam.py` | Paytm auth; Sarvam streaming; multilingual embeddings + vector index |
| Step 2 Diagnose | `saathi/steps/step2_diagnose.py`, `saathi/integrations/llm.py` | Bayes over tests, with learned likelihoods; a self-hosted open model (the prototype uses Gemini) |
| Step 3 Safety | `saathi/steps/step3_safety.py` | OPA/Rego policy engine; KMS-backed token signer |
| Step 4 Act | `saathi/steps/step4_act.py` | Action gateway; immutable audit ledger; saga compensations |
| Step 5 Contract | `saathi/steps/step5_contract.py`, `saathi/services/notify.py` *(added)* | Contracts in Postgres; Temporal durable timers; WhatsApp/app push |
| Step 6 Verify | `saathi/steps/step6_verify.py`, `saathi/core/event_bus.py` | Kafka consumers + timers; versioned predicate registry |
| Step 7 Remediate | `saathi/steps/step7_remediate.py`, `saathi/services/human_desk.py` *(added)* | Temporal retries/compensation; ops ticketing |
| Learning loop | `saathi/services/learning.py` | Offline training, feature store, streaming incident detection |
| MCP | `saathi/mcp/mcp_server.py`, `saathi/mcp/mcp_client.py` | Official MCP SDK server/client with OAuth |
| Systems of record (sim) | `saathi/sim/paytm_sim.py`, `saathi/sim/seed.py`, `saathi/core/store.py` | Paytm's real services, each with its own DB |
| Time | `saathi/core/clock.py` | Temporal durable timers |
| Observability | `saathi/core/logger.py`, `saathi/core/trace.py` | OpenTelemetry logs and spans |

**MCP.** This is a minimal MCP-style server (initialize, tools/list, tools/call); production would use a full MCP server via the official SDK in front of Paytm's systems.
- **Read tools:** `get_state_card, get_devices, get_charges, get_tickets, get_billing_events, get_warehouse_scan, get_settlements, get_ledger_entries, get_device_heartbeat, get_payments`, plus three added tools: `get_payouts`, `get_credit_queue` (used by the Step 7 re-check) and `is_linked_account` (answers the policy question without exposing account numbers).
- **Write tools:** `stop_rental, post_credit, requeue_settlement, remote_restart`. Each needs a valid gateway token, and **the server re-verifies that token**, so a write that bypasses the gateway still fails.
- **Scope:** the server takes `merchant_id` from the session, rejects any other merchant's id, and also rejects row ids that belong to another merchant.
- **Data access:** `saathi/mcp/mcp_server.py` is the only Saathi-side module that queries merchant tables. A test enforces this. `saathi/sim/paytm_sim.py` is the Paytm side, so it owns and writes those tables.

---

## The demo world (all fictional)

The virtual clock starts at **2026-11-14 10:00 IST**, and all seed dates are relative to that start. The top bar's **+1h / +24h / +48h** buttons fire scheduled system events in timestamp order.

| Scenario | Merchant | What happens |
|---|---|---|
| S1 (te) | M-2291 Ramesh, kirana, Warangal | INC-0412 by key; T-188 was closed without credit, so priority is HIGH. Bayes confidence 0.98 with **no LLM**. ALLOW, 2 actions, 48h contract. At +24h the rental check passes. At +48h the credit check fails because the refund queue is stuck, and the full ladder runs. A human resolves it on the desk, the credit posts, and the case closes with proof. |
| S2 (hi) | M-3107 Lakshmi | Same incident from different words in a different language. The credit posts at +30h and the case closes. The "done" message is in Hindi. |
| S3 (hi) | M-4420 Imran | Two open items with a TF-IDF margin of 0.06 (below 0.15), so Saathi sends a one-tap choice. Choosing the settlement attaches INC-0420. ₹18,400 is above the ₹10,000 playbook threshold, so the case needs approval. After approval the settlement credits in 6h and the case closes. |
| S4 (hinglish) | M-5150 Sunita | No anomaly, so the case is unmatched and the area resolves to payments. The Tier 2 tool loop finds that SB-91 is offline while payments still credit. Novel cases need approval. After `remote_restart` the heartbeat returns in 15 min, the case closes, and a **draft playbook** goes to the desk. |
| S5 (hi) | M-6001 Vikram | The "I'm the owner" claim is ignored. **DENY** because the destination is an unlinked account and there is no disputed charge. The case is flagged to the desk and Vikram gets a refusal in Hindi. Nothing is executed. |
| Spike | 40 DCCB merchants | "Simulate bank outage" creates 40 `settlement_held` tickets (reason BANK_TIMEOUT) within 2h. Spike detection (at least 10 merchants and at least 5× baseline) opens **one** incident: 40 tickets → 1 incident. |

The historical data has 120 rows. They are tuned so that the rent_after_return priors are exactly 0.70 / 0.15 / 0.10 / 0.05, credit latency P90 is 36h (deadline 48h), and settlement latency P90 is 18h (deadline 24h).

---

## Choices and deviations

- **Sunita's last heartbeat** is seeded at **09:00 today**, not 11:00, because the clock starts at 10:00. The simulator keeps crediting her QR payments every 2h of virtual time with alerts marked "not sent", which keeps her case novel even after the clock moves forward.
- **Ramesh's goodwill credit (₹50)** goes through the same policy, token and gateway path as any other action.
- **RBI note:** for failed UPI payments, the RBI turnaround-time rule is reversal within T+1 and ₹100/day compensation after that. Saathi's ₹50 is a separate merchant-promise policy, not that rule.
- **There are 7 policy rules**, not 6, so the S1 log line reads "7/7 rules ✓": target ownership, no payout to an unlinked account, credit ≤ disputed charge, fraud flag, auto-credit cap ₹1,000, at most 2 credits per 30 days, and playbook autonomy (novel cases need approval; ACT_WITH_APPROVAL applies above the playbook threshold).
- **S1 confidence is 0.977**, close to the spec's "~0.97". It comes from the likelihood table in `saathi/steps/step2_diagnose.py`.
- **Outage incident:** the outage uses reason `BANK_TIMEOUT`, so spike detection opens a *new* incident instead of joining INC-0420. A pool of 34 extra fictional DCCB merchants (M-8001…) exists only for this button.
- **Added modules:** `saathi/services/pipeline.py`, `saathi/services/notify.py` and `saathi/services/human_desk.py` were added for clarity.
- **SQLite** runs in memory (`SAATHI_DB` can point to a file). Reset re-seeds the world.
- **Mock latency:** mock latencies in the logs are the real elapsed time of the mock code (often under 1 ms). They are never invented numbers.
- **Message templates** cover 4 languages (te, hi, hinglish, en) × 11 kinds in `saathi/sim/seed.py`. The te/hi lines quoted in the brief are used verbatim. **All other non-English templates need native review**, in particular every Telugu line except promise/delay/done, every Hindi line except promise/done/refusal, and all Hinglish lines.

## Prototype vs production

| Concern | Prototype | Production |
|---|---|---|
| Policy | Python rule functions | OPA/Rego over the same dry-run diff |
| Timers / durability | `saathi/core/clock.py` heap + in-process bus | Temporal + Kafka |
| Reasoning model | Gemini (OpenAI-compatible) or mock | Self-hosted open model fine-tuned on resolved cases |
| Similarity | Hand-written TF-IDF | Multilingual embeddings + vector index |
| Tools | Minimal MCP-style JSON-RPC server | Official MCP SDK server with OAuth service accounts |
| Audit | sha256 hash chain in SQLite | Append-only ledger / QLDB-style table |
| Data | One in-memory SQLite DB | Paytm systems of record + Saathi's own Postgres |
