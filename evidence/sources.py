"""Sources registry schema and inline-tag parser (design decision #4).

A canonical registry of every ingested source (``sources.jsonl``). Inline provenance tags in memory
bullets — e.g. ``[doc:e037·A]`` — resolve back to a full source record here (label, category, tier,
recency, scope-fit). The registry append is idempotent by ``ref``; tag resolution is read-only.

Tag grammar
-----------
    [doc:<ref>·<tier>]   first-party / authoritative document   (carries ref + tier)
    [web:<ref>·<tier>]   web / aggregated source                (carries ref + tier)
    [user]               user statement                         (tier fixed = F)
    [ai]                 model inference                        (tier fixed = G)

Only ``doc`` / ``web`` tags carry a source ref + tier; ``user`` / ``ai`` are tier-fixed by *kind*. The
separator between ref and tier is the middle dot ``·`` (U+00B7).

Publication boundary
--------------------
The schema *shape* and the tag→source resolution flow are published. The production **enrichment
heuristics** that score a source's ``independence`` / ``recency`` / ``scope_fit`` are withheld — those
fields exist in the schema but are populated by callers, not computed here.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evidence.tiers import resolve_tier

# Tier a tag kind is *fixed* to by its kind alone (no ref needed).
_KIND_FIXED_TIER: dict[str, str] = {"user": "F", "ai": "G"}

# [kind:ref·tier]  or  [kind]   — the middle dot (·, U+00B7) separates ref and tier.
_TAG_RE = re.compile(r"\[(?P<kind>[a-z]+)(?::(?P<ref>[^\]·]+)·(?P<tier>[A-G]))?\]")


@dataclass(frozen=True)
class Source:
    """One row of the sources registry.

    ``ref`` / ``label`` / ``category`` / ``tier`` are the published core. ``effective_date`` and the
    enrichment fields (``independence`` / ``recency`` / ``scope_fit``) are part of the schema *shape*;
    how they are *scored* in production is withheld, so they default to ``None``.
    """

    ref: str                       # stable id, e.g. "e037"
    label: str                     # human-readable name
    category: str                  # resolves to a tier via evidence.tiers.resolve_tier
    tier: str                      # A-G (authoritative copy of the tier; inline tags reconcile to it)
    effective_date: str | None = None
    # Enrichment shape only — scoring logic stays in-house.
    independence: float | None = None
    recency: float | None = None
    scope_fit: float | None = None

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None})


def build_tag(kind: str, ref: str | None = None, tier: str | None = None) -> str:
    """Render an inline provenance tag.

    ``build_tag("user")`` -> ``"[user]"``; ``build_tag("doc", "e037", "A")`` -> ``"[doc:e037·A]"``.
    """
    if kind in _KIND_FIXED_TIER:
        return f"[{kind}]"
    if not ref or not tier:
        raise ValueError(f"{kind!r} tags require both ref and tier")
    return f"[{kind}:{ref}·{tier}]"


def parse_tag(tag: str) -> tuple[str, str | None, str | None]:
    """Parse one inline provenance tag into ``(kind, ref, tier)``.

    ``[doc:e037·A]`` -> ``("doc", "e037", "A")``; ``[user]`` -> ``("user", None, "F")`` (tier is fixed by
    kind for user/ai). Raises ``ValueError`` if the tag is malformed.
    """
    m = _TAG_RE.fullmatch(tag.strip())
    if not m:
        raise ValueError(f"malformed provenance tag: {tag!r}")
    kind = m.group("kind")
    if kind in _KIND_FIXED_TIER:
        return kind, None, _KIND_FIXED_TIER[kind]
    if m.group("ref") is None:
        raise ValueError(f"{kind!r} tag must carry ref·tier: {tag!r}")
    return kind, m.group("ref"), m.group("tier")


def iter_tags(text: str) -> list[tuple[str, str | None, str | None]]:
    """Find every inline provenance tag in a block of text, in order."""
    return [parse_tag(m.group(0)) for m in _TAG_RE.finditer(text)]


class SourcesRegistry:
    """File-backed ``sources.jsonl`` registry. Append is idempotent by ``ref``; lookups are read-only."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._by_ref: dict[str, Source] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._load_row(json.loads(line))

    def _load_row(self, row: dict) -> None:
        self._by_ref[row["ref"]] = Source(**row)

    def register(self, source: Source) -> Source:
        """Append a source. Idempotent: a ref already present is left unchanged and returned as-is.

        Use :meth:`update` to change an existing source's grading (which the consolidator reconciles
        inline tags against).
        """
        existing = self._by_ref.get(source.ref)
        if existing is not None:
            return existing
        self._by_ref[source.ref] = source
        self._append(source)
        return source

    def update(self, ref: str, **changes) -> Source:
        """Re-grade an existing source (e.g. its ``tier``) and rewrite the registry file.

        Models a source whose standing changed as evidence about *it* accumulated. The consolidator (#6)
        then reconciles stale inline tags in the corpus against this authoritative copy.
        """
        current = self.lookup(ref)
        updated = Source(**{**asdict(current), **changes})
        self._by_ref[ref] = updated
        self._rewrite()
        return updated

    def lookup(self, ref: str) -> Source:
        """Resolve a source ref to its full registry record. Raises ``KeyError`` if unknown."""
        try:
            return self._by_ref[ref]
        except KeyError as exc:
            raise KeyError(f"no source registered for ref {ref!r}") from exc

    def get(self, ref: str) -> Source | None:
        """Like :meth:`lookup` but returns ``None`` for an unknown ref."""
        return self._by_ref.get(ref)

    def list_sources(self) -> list[Source]:
        return list(self._by_ref.values())

    # --- persistence -----------------------------------------------------------------------------
    def _append(self, source: Source) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(source.to_json() + "\n")

    def _rewrite(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(s.to_json() + "\n" for s in self._by_ref.values()), encoding="utf-8"
        )


def source_from_category(ref: str, label: str, category: str, **extra) -> Source:
    """Build a :class:`Source`, deriving its tier from the category via the tier resolver."""
    return Source(ref=ref, label=label, category=category, tier=resolve_tier(category), **extra)
