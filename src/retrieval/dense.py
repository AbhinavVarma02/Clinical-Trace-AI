"""Dense embedding backends behind one small interface.

``LSAEmbedder`` (TF-IDF word/char n-grams -> truncated SVD, L2-normalised) is
the default: local, deterministic, CPU-only, and free. It is a classical latent
semantic baseline, not a neural biomedical encoder; comparison against neural
general-purpose and biomedical encoders is tracked as roadmap work
(``docs/IMPLEMENTATION_ROADMAP.md``, CT-P13-T02).
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
from scipy.sparse import hstack
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer


class Embedder(Protocol):
    name: str

    def fit(self, texts: list[str]) -> "Embedder": ...

    def encode(self, texts: list[str]) -> np.ndarray: ...


class LSAEmbedder:
    def __init__(self, dimensions: int = 96, seed: int = 7) -> None:
        self.dimensions = dimensions
        self.seed = seed
        self.name = f"lsa-tfidf-svd-{dimensions}"
        self._word = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1)
        self._char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=1)
        self._svd: TruncatedSVD | None = None

    def fit(self, texts: list[str]) -> "LSAEmbedder":
        matrix = hstack([self._word.fit_transform(texts), self._char.fit_transform(texts)]).tocsr()
        components = max(2, min(self.dimensions, matrix.shape[0] - 1, matrix.shape[1] - 1))
        self._svd = TruncatedSVD(n_components=components, random_state=self.seed)
        self._svd.fit(matrix)
        return self

    def encode(self, texts: list[str]) -> np.ndarray:
        if self._svd is None:
            raise RuntimeError("embedder is not fitted")
        matrix = hstack([self._word.transform(texts), self._char.transform(texts)]).tocsr()
        vectors = self._svd.transform(matrix)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms
