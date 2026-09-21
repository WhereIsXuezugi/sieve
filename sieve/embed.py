"""Pluggable similarity vectors.

The default backend is the hashed TF-IDF in ``textutil``: no model, no
download, no GPU, and good enough that "more like this" behaves sensibly across
a personal library of tens of thousands of videos. This module exists so that
claim can be tested rather than asserted — swap the backend, rescore, and
compare.

Backends
    hashed  (default) sparse term vectors, pure Python, ~40 µs per video
    ollama            dense embeddings from a local Ollama model
    openai            dense embeddings from any OpenAI-compatible endpoint

Everything downstream consumes a ``Vector`` — a dict of name to float, L2
normalised — and computes cosine over it. Dense vectors are stored in the same
shape with keys ``d0``, ``d1``, …, which costs roughly eight times the bytes of
a sparse vector in SQLite and buys nothing on a small library. That tradeoff is
the whole reason the default is what it is; it is not hidden behind an
abstraction, it is just the number above.

Switching backend invalidates every stored vector, so ``sieve score --all`` has
to run afterwards. `SCORER_VERSION` in scoring.py is bumped by the backend name
so this is enforced rather than remembered.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

import httpx

from . import textutil as T
from .config import Config

log = logging.getLogger("sieve.embed")

Vector = dict[str, float]


class Embedder:
    """One object, three backends, one method the rest of the code calls."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.provider = (cfg.embed_provider or "hashed").lower()
        self._client: httpx.Client | None = None
        self._warned = False

    # -- the only method anything else uses -------------------------------

    def vector(self, parts: Iterable[tuple[str, float]], top_k: int = 64) -> Vector:
        parts = list(parts)
        if self.provider == "hashed":
            return T.term_vector(parts, top_k)
        text = " ".join(t for t, w in parts if t and w > 0)[:6000]
        if not text.strip():
            return {}
        try:
            dense = self._dense(text)
        except Exception as exc:
            # A missing model must degrade, not break the homepage.
            if not self._warned:
                log.warning("embedding backend %s unavailable (%s); "
                            "falling back to hashed vectors", self.provider, exc)
                self._warned = True
            return T.term_vector(parts, top_k)
        return densify(dense)

    def available(self) -> bool:
        if self.provider == "hashed":
            return True
        try:
            self._dense("probe")
            return True
        except Exception:
            return False

    # -- backends ----------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=max(30.0, self.cfg.request_timeout * 3))
        return self._client

    def _dense(self, text: str) -> Sequence[float]:
        base = (self.cfg.embed_base_url or self.cfg.llm_base_url).rstrip("/")
        if self.provider == "ollama":
            response = self.client.post(
                f"{base}/api/embeddings",
                json={"model": self.cfg.embed_model, "prompt": text},
            )
            response.raise_for_status()
            return response.json()["embedding"]
        if self.provider == "openai":
            response = self.client.post(
                f"{base}/v1/embeddings",
                headers={"Authorization": f"Bearer {self.cfg.llm_api_key}"},
                json={"model": self.cfg.embed_model, "input": text},
            )
            response.raise_for_status()
            return response.json()["data"][0]["embedding"]
        raise ValueError(f"unknown embedding provider {self.provider!r}")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def densify(values: Sequence[float]) -> Vector:
    """Dense list -> the sparse dict shape the rest of the code speaks.

    Near-zero dimensions are dropped: it costs a little accuracy and saves a
    lot of database, and cosine over the survivors is within a percent of the
    full vector for the models people actually use here.
    """
    if not values:
        return {}
    cutoff = 0.02 * max(abs(v) for v in values)
    vec = {f"d{i}": float(v) for i, v in enumerate(values) if abs(v) > cutoff}
    return T.normalise(vec)


def is_dense(vector: Vector) -> bool:
    """Dense and sparse vectors must never be compared — cosine between them is
    meaningless, not merely inaccurate."""
    for key in vector:
        return key.startswith("d") and key[1:].isdigit()
    return False


def compatible(a: Vector, b: Vector) -> bool:
    return not a or not b or is_dense(a) == is_dense(b)
