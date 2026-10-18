"""Tokenisation and Okapi BM25 lexical scoring (dependency-free)."""

from __future__ import annotations

import math
import re
from collections import Counter

TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it of on or the to was were with what which when "
    "who how did does do this that these those patient patients please show tell me about any over time "
    "change changed changes say says said value values record records ever been since there their his her "
    "they them its can could would should will also than then into".split()
)
# Light normalisation of common clinical abbreviations and plurals.
SYNONYMS = {
    "a1c": "hba1c", "hb": "hemoglobin", "meds": "medication",
    "medications": "medication", "drugs": "medication", "labs": "lab", "readmitted": "readmission",
    "readmissions": "readmission", "admissions": "admission", "admitted": "admission",
    "kidney": "egfr", "renal": "egfr", "smoking": "smoker", "smokes": "smoker", "cigarettes": "smoker",
    "sugar": "glucose", "anticoagulation": "warfarin", "anticoagulant": "warfarin",
    "notes": "note", "stopped": "stop", "discontinued": "stop", "summaries": "summary",
}


def tokenize(text: str) -> list[str]:
    tokens = []
    for token in TOKEN.findall(text.lower()):
        if token in STOPWORDS:
            continue
        token = SYNONYMS.get(token, token)
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.append(token)
    return tokens


class BM25:
    def __init__(self, documents: list[list[str]], k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(doc.keys())
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        results = []
        for doc, length in zip(self.docs, self.lengths):
            score = 0.0
            for term in query:
                tf = doc.get(term)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self.avg_len or 1.0))
                score += self.idf.get(term, 0.0) * tf * (self.k1 + 1) / denom
            results.append(score)
        return results
