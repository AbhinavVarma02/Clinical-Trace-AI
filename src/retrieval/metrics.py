"""Standard ranking metrics over source references (binary relevance)."""

from __future__ import annotations

import math


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 1.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    top = ranked[:k]
    if not top:
        return 0.0
    return sum(1 for item in top if item in relevant) / len(top)


def mrr_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    for rank, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(rank + 1) for rank, item in enumerate(ranked[:k], start=1) if item in relevant)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered
