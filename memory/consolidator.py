"""Slim consolidation pass — async path for temporal re-grading (decisions #5 async, #6).

The scheduled backstop that keeps the corpus coherent. This reference version is deliberately slim and
does two mechanical, idempotent things:

  1. **Dedup.** Remove exact-duplicate bullets *within a section* (two extraction passes writing the same
     fact). Only verbatim duplicates are dropped — annotated, confirmed, and superseded bullets are
     distinct text and are preserved, so the audit trail (#5) is never lost.

  2. **Re-tier via the registry.** Reconcile each inline ``[doc:ref·X]`` / ``[web:ref·X]`` tag against
     the *authoritative* tier the sources registry holds for that ref (``lookup_source``). When a source
     is re-graded — or the runtime tagged a bullet with the wrong tier — the consolidator corrects the
     inline tag to match. The registry is the single source of authority for a source's standing.

Both passes are **non-destructive** (no fact is deleted; supersession is recorded, not erased) and
**idempotent** (running twice produces no further change). Re-running yields an empty report.

Described-only (not in this runnable core): task archival, profile dedup, topic-reorg overflow handling,
and the production LLM-judgment merge prompts. See docs/architecture.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from evidence.sources import SourcesRegistry
from memory.store import MemoryStore

_H2_LINE_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_LINE_RE = re.compile(r"^\s*[-*]\s+")
# [doc:ref·tier] / [web:ref·tier] — capture the parts so we can rewrite only the tier.
_DOCWEB_TAG_RE = re.compile(r"\[(doc|web):([^\]·]+)·([A-G])\]")


@dataclass
class ConsolidationReport:
    """What one consolidation pass changed. Empty across the board == corpus already coherent."""

    deduped: list[str] = field(default_factory=list)            # bullet text removed as duplicate
    retiered: list[tuple[str, str, str]] = field(default_factory=list)  # (ref, old_tier, new_tier)

    @property
    def changed(self) -> bool:
        return bool(self.deduped or self.retiered)


def consolidate(store: MemoryStore, registry: SourcesRegistry) -> ConsolidationReport:
    """One non-destructive, idempotent dedup + re-tier pass over the topic corpus.

    Rewrites the markdown source of truth in place, then rebuilds the derived FTS5 index so subsequent
    recall reflects the reconciled tiers.
    """
    report = ConsolidationReport()

    for topic in store.list_topics():
        original = store.read_topic(topic)
        text = _retier_tags(original, registry, report)
        text = _dedup_sections(text, report)
        if text != original:
            store.write_topic(topic, text)

    if report.changed:
        store.rebuild_index()
    return report


def _retier_tags(markdown: str, registry: SourcesRegistry, report: ConsolidationReport) -> str:
    """Rewrite each doc/web inline tag's tier to match the registry's authoritative tier for that ref."""

    def replace(m: re.Match) -> str:
        kind, ref, inline_tier = m.group(1), m.group(2), m.group(3)
        source = registry.get(ref)
        if source is None or source.tier == inline_tier:
            return m.group(0)
        report.retiered.append((ref, inline_tier, source.tier))
        return f"[{kind}:{ref}·{source.tier}]"

    return _DOCWEB_TAG_RE.sub(replace, markdown)


def _dedup_sections(markdown: str, report: ConsolidationReport) -> str:
    """Drop verbatim-duplicate bullet lines within each H2 section, preserving first occurrence + order."""
    out: list[str] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        if _H2_LINE_RE.match(line):
            seen = set()  # duplicates are scoped to a section; reset at each heading
            out.append(line)
            continue
        if _BULLET_LINE_RE.match(line):
            key = line.strip()
            if key in seen:
                report.deduped.append(key)
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")
