"""Semantic search layer over topic sections (design decision #1, semantic path).

The vector half of dual-path recall: embeds each H2 section, scores a query by cosine similarity, and
returns ranked matches for the injector to merge with the keyword results. Like the FTS5 index, this is a
**derived view** over the markdown source of truth and can be rebuilt at will.

Embedder is pluggable
---------------------
Production embeds with a hosted model (``text-embedding-3-small``) — and strips the usage/billing
plumbing that wrapped it (publication boundary). To keep this reference **runnable with zero
dependencies and no network/API key**, the default :class:`HashingEmbedder` is a deterministic local
stand-in: a hashed bag of word tokens + character trigrams, L2-normalized. It captures lexical and
morphological proximity (``permit`` ≈ ``permitting``) — enough to demonstrate the *architecture* (cosine
ranking over section vectors, merged with the keyword path). Swap in a real model by passing any object
with an ``embed(text) -> list[float]`` method; nothing else changes.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from memory.store import Section, SectionMatch

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Deterministic, dependency-free embedder (local stand-in for a hosted embedding model).

    Hashes word tokens *and* character trigrams into a fixed-dimension signed vector, then L2-normalizes.
    Trigrams give morphological/fuzzy proximity so the semantic path adds signal a pure exact-token
    keyword index would miss. Not a substitute for a real embedding model — a runnable illustration of
    where one plugs in.
    """

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        feats: list[str] = []
        for tok in _TOKEN_RE.findall(text.lower()):
            feats.append(f"w:{tok}")
            padded = f"#{tok}#"
            for i in range(len(padded) - 2):
                feats.append(f"t:{padded[i : i + 3]}")
        return feats

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feat in self._features(text):
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 1) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both are L2-normalized


@dataclass
class _Indexed:
    section: Section
    vector: list[float]


class SemanticIndex:
    """Cosine vector search over topic sections — the semantic half of dual-path recall."""

    def __init__(self, embedder: Embedder | None = None):
        self.embedder: Embedder = embedder or HashingEmbedder()
        self._items: list[_Indexed] = []

    def rebuild(self, sections: list[Section]) -> None:
        """Re-embed all topic sections from the markdown source of truth."""
        self._items = [
            _Indexed(s, self.embedder.embed(f"{s.topic} {s.heading}\n{s.body}")) for s in sections
        ]

    def search(self, query: str, limit: int = 8) -> list[SectionMatch]:
        """Cosine-ranked section matches for a query (the semantic half of dual-path recall)."""
        q = self.embedder.embed(query)
        scored = [SectionMatch(it.section, relevance=_cosine(q, it.vector)) for it in self._items]
        scored.sort(key=lambda m: m.relevance, reverse=True)
        return scored[:limit]
