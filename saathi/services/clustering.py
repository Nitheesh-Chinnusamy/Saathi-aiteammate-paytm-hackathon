"""clustering.py - hand-rolled TF-IDF vectoriser + nearest cluster / area / incident scoring with margins.

Corpus = cluster-card example phrases + descriptions, area descriptions, incident descriptions.
Score of a cluster = max cosine over its example phrases and description ("nearest example").
Margin = top1 - top2; small margins trigger a choice question instead of a guess.

Production: multilingual sentence embeddings (self-hosted) + a vector index; TF-IDF is the
cheap, explainable prototype stand-in.
"""
import json
import math
import re
from collections import Counter

from saathi.core import store
from saathi.sim.seed import AREAS

STOP = {"the", "a", "an", "is", "are", "am", "was", "were", "be", "been", "to", "of", "in", "on", "for", "and", "or",
        "but", "my", "me", "i", "it", "this", "that", "you", "your", "we", "our", "from", "with", "at", "by", "so",
        "still", "yet", "has", "have", "had", "after", "even", "why", "where", "what", "being", "do", "did", "does",
        "im", "its", "through", "getting", "get", "please", "last", "week", "two", "days"}


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def tokenize(text):
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"n't\b", " not", t).replace("'s", "")
    words = re.findall(r"[a-z0-9]+", t)
    return [_stem(w) for w in words if w not in STOP and len(w) > 1]


class TfIdf:
    def __init__(self, docs):
        self.n = len(docs)
        df = Counter()
        for d in docs:
            df.update(set(tokenize(d)))
        self.idf = {w: math.log((1 + self.n) / (1 + c)) + 1.0 for w, c in df.items()}

    def vec(self, text):
        tf = Counter(tokenize(text))
        v = {w: c * self.idf.get(w, math.log(1 + self.n) + 1.0) for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}

    @staticmethod
    def cos(a, b):
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(w, 0.0) for w, x in a.items())


_model = {"tfidf": None}


def _cards():
    rows = store.q("SELECT issue_type, area, description, examples FROM cluster_cards")
    for r in rows:
        r["examples"] = json.loads(r["examples"])
    return rows


def model():
    if _model["tfidf"] is None:
        docs = []
        for c in _cards():
            docs += c["examples"] + [c["description"]]
        docs += list(AREAS.values())
        docs += [r["description"] for r in store.q("SELECT description FROM incidents")]
        _model["tfidf"] = TfIdf(docs)
    return _model["tfidf"]


def invalidate():
    _model["tfidf"] = None


def _rank(text, named_docs):
    m = model()
    v = m.vec(text)
    scores = {name: max((m.cos(v, m.vec(d)) for d in docs), default=0.0) for name, docs in named_docs.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0) if ranked else 0.0
    return [(k, round(s, 3)) for k, s in ranked], round(margin, 3)


def score_clusters(text, issue_types=None):
    named = {c["issue_type"]: c["examples"] + [c["description"]] for c in _cards()
             if issue_types is None or c["issue_type"] in issue_types}
    return _rank(text, named)


def score_areas(text):
    return _rank(text, {a: [d] for a, d in AREAS.items()})


def score_incidents(text, incidents):
    if not incidents:
        return [], 0.0
    return _rank(text, {i["id"]: [i["description"], i["title"]] for i in incidents})


def fmt(ranked, margin, n=2):
    return " · ".join(f"{k} {s:.2f}" for k, s in ranked[:n]) + f" · margin {margin:.2f}"
