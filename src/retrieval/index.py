"""Patient-partitioned hybrid retrieval index with RRF fusion, caching, and diagnostics.

Isolation is structural: each patient has its own partition (chunks, BM25
statistics, embedding matrix) and ``search`` only ever reads the partition of
the requested patient. A post-condition check re-verifies that every returned
chunk belongs to that patient and fails closed otherwise.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Literal

import numpy as np

from src.clinical.fhir_models import parse_timestamp
from src.clinical.records import ClinicalRecords
from src.retrieval.chunking import Chunk, chunk_patient
from src.retrieval.dense import Embedder, LSAEmbedder
from src.retrieval.lexical import BM25, tokenize

Mode = Literal["bm25", "dense", "hybrid"]
RRF_K = 60


class IsolationError(RuntimeError):
    """Raised if a retrieval result would cross a patient boundary."""


@dataclass
class _Partition:
    chunks: list[Chunk]
    bm25: BM25
    vectors: np.ndarray


class RetrievalIndex:
    def __init__(
        self,
        records: ClinicalRecords,
        embedder_factory: Callable[[], Embedder] = LSAEmbedder,
        flagger: Callable[[str], list[str]] | None = None,
        cache_size: int = 256,
    ) -> None:
        self.records = records
        self.embedder_factory = embedder_factory
        self.flagger = flagger
        self._lock = threading.RLock()
        self._partitions: dict[str, _Partition] = {}
        self._embedder: Embedder | None = None
        self._cache: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
        self._cache_size = cache_size
        self.cache_hits = 0
        self.cache_misses = 0
        self.version = "empty"
        self.built_at: float | None = None

    # ----------------------------------------------------------------- build
    def build(self) -> None:
        with self._lock:
            patient_chunks = {
                pid: chunk_patient(self.records, pid, self.flagger)
                for pid in self.records.store.patient_ids()
            }
            corpus = [c.text for chunks in patient_chunks.values() for c in chunks]
            embedder = self.embedder_factory()
            if corpus:
                embedder.fit(corpus)
            partitions = {}
            for pid, chunks in patient_chunks.items():
                vectors = embedder.encode([c.text for c in chunks]) if chunks else np.zeros((0, 1))
                partitions[pid] = _Partition(chunks, BM25([tokenize(c.text) for c in chunks]), vectors)
            digest = hashlib.sha256()
            digest.update(embedder.name.encode())
            for pid in sorted(patient_chunks):
                for chunk in patient_chunks[pid]:
                    digest.update(f"{chunk.chunk_id}:{chunk.content_hash}".encode())
            self._partitions = partitions
            self._embedder = embedder
            self._cache.clear()
            self.version = digest.hexdigest()[:12]
            self.built_at = time.time()

    def refresh(self, _changed_patients: set[str] | None = None) -> None:
        """Rebuild after ingestion. The corpus is small, so a full refit keeps embeddings consistent."""
        self.build()

    # ---------------------------------------------------------------- search
    def chunk(self, patient_id: str, chunk_id: str) -> Chunk | None:
        partition = self._partitions.get(patient_id)
        if partition is None:
            return None
        return next((c for c in partition.chunks if c.chunk_id == chunk_id), None)

    def chunks_for(self, patient_id: str) -> list[Chunk]:
        partition = self._partitions.get(patient_id)
        return list(partition.chunks) if partition else []

    def search(
        self,
        patient_id: str,
        query: str,
        k: int = 6,
        mode: Mode = "hybrid",
        date_from: str | None = None,
        date_to: str | None = None,
        source_types: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        key = (patient_id, query.strip().lower(), k, mode, date_from, date_to, source_types, self.version)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self.cache_hits += 1
                return self._response(patient_id, query, mode, cached, started, cache_hit=True)
            self.cache_misses += 1
            partition = self._partitions.get(patient_id)
            embedder = self._embedder
        if partition is None or not partition.chunks or embedder is None:
            return self._response(patient_id, query, mode, [], started, cache_hit=False)

        candidates = list(range(len(partition.chunks)))
        if date_from or date_to:
            lower = parse_timestamp(date_from) if date_from else None
            upper = parse_timestamp(date_to) if date_to else None
            candidates = [
                i for i in candidates
                if partition.chunks[i].date
                and (lower is None or parse_timestamp(partition.chunks[i].date) >= lower)
                and (upper is None or parse_timestamp(partition.chunks[i].date) <= upper)
            ]
        if source_types:
            candidates = [i for i in candidates if partition.chunks[i].source_type.startswith(source_types)]

        bm25_scores = partition.bm25.scores(tokenize(query))
        dense_scores = (partition.vectors @ embedder.encode([query])[0]).tolist()
        bm25_rank = self._ranks([i for i in candidates if bm25_scores[i] > 0], bm25_scores)
        dense_rank = self._ranks(candidates, dense_scores)

        fused: dict[int, float] = {}
        if mode in ("bm25", "hybrid"):
            for i, rank in bm25_rank.items():
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
        if mode in ("dense", "hybrid"):
            for i, rank in dense_rank.items():
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
        ordered = sorted(fused, key=lambda i: (-fused[i], partition.chunks[i].chunk_id))[:k]
        results = []
        for i in ordered:
            chunk = partition.chunks[i]
            if chunk.patient_id != patient_id:  # structural guarantee; fail closed if violated
                raise IsolationError("retrieval partition contained another patient's chunk")
            results.append({
                **chunk.as_dict(),
                "scores": {
                    "fused": round(fused[i], 6),
                    "bm25": round(bm25_scores[i], 4),
                    "bm25_rank": bm25_rank.get(i),
                    "dense": round(dense_scores[i], 4),
                    "dense_rank": dense_rank.get(i),
                },
            })
        with self._lock:
            self._cache[key] = results
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return self._response(patient_id, query, mode, results, started, cache_hit=False)

    @staticmethod
    def _ranks(indices: list[int], scores: list[float]) -> dict[int, int]:
        ordered = sorted(indices, key=lambda i: -scores[i])
        return {i: rank for rank, i in enumerate(ordered, start=1)}

    def _response(
        self, patient_id: str, query: str, mode: str, results: list[dict[str, Any]], started: float, cache_hit: bool
    ) -> dict[str, Any]:
        return {
            "patient_id": patient_id,
            "query": query,
            "mode": mode,
            "results": results,
            "diagnostics": {
                "index_version": self.version,
                "embedder": self._embedder.name if self._embedder else None,
                "fusion": f"reciprocal-rank-fusion(k={RRF_K})" if mode == "hybrid" else mode,
                "partition_size": len(self.chunks_for(patient_id)),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "cache_hit": cache_hit,
            },
        }

    def stats(self) -> dict[str, Any]:
        return {
            "index_version": self.version,
            "embedder": self._embedder.name if self._embedder else None,
            "patients": len(self._partitions),
            "chunks": sum(len(p.chunks) for p in self._partitions.values()),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "built_at": self.built_at,
        }
