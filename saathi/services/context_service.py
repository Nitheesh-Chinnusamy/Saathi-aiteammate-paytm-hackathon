"""context_service.py - builds the masked per-merchant STATE CARD via mcp_client.

The AI never touches the database: it only ever sees this card (and read-only tool results).
The card is validated here so a malformed card never reaches the reasoning steps.

Production: a context/feature service with caching and field-level PII policies.
"""
from saathi.mcp.mcp_client import MCPClient

REQUIRED = ("merchant", "as_of", "devices", "charges_30d", "settlements_7d", "tickets_90d", "open_anomalies")


def get_state_card(merchant_id, client=None):
    client = client or MCPClient(merchant_id)
    card = client.call("get_customer_card" if merchant_id.startswith("C-") else "get_state_card", {})
    missing = [k for k in REQUIRED if k not in card]
    if missing:
        raise ValueError(f"state card missing {missing}")
    if card["merchant"]["id"] != merchant_id:
        raise ValueError("state card for wrong merchant")
    return card
