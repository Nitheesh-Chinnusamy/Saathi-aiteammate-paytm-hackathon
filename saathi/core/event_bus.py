"""event_bus.py - in-process synchronous pub/sub.

Simulated Paytm systems publish domain events (sim.rental_stopped, sim.credit_posted ...),
the gateway publishes actions.executed, and Step 6 subscribes to verify contracts.

Production: Kafka / Paytm's event streaming platform with durable consumers.
"""
import threading
from collections import defaultdict

_subs = defaultdict(list)
_lock = threading.RLock()


def subscribe(topic, fn):
    """topic may be exact ('sim.credit_posted'), a prefix ('sim.*') or '*'."""
    with _lock:
        if fn not in _subs[topic]:
            _subs[topic].append(fn)


def publish(topic, payload=None):
    payload = dict(payload or {})
    payload.setdefault("topic", topic)
    with _lock:
        targets = list(_subs.get(topic, []))
        for pattern, fns in _subs.items():
            if pattern == "*" or (pattern.endswith(".*") and topic.startswith(pattern[:-1])):
                targets.extend(fns)
    for fn in targets:
        try:
            fn(topic, payload)
        except Exception as e:
            from saathi.core import logger
            logger.log("verify", f"subscriber {getattr(fn, '__name__', fn)} failed on {topic}: {e!r}", level="error")
    return len(targets)


def clear():
    with _lock:
        _subs.clear()
