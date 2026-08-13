"""Slim consolidation pass — async path for temporal re-grading (decisions #5 async, #6) and link
integrity (#9).

The scheduled backstop that keeps the corpus coherent. This reference version is deliberately slim and
does three mechanical, idempotent things:

  1. **Dedup.** Remove duplicate bullets *within a section* (two extraction passes writing the same
     fact) using a punctuation-insensitive key — ``tailwindcss: ^4.3.2`` and ``tailwindcss@^4.3.2`` are
     the same fact, reworded, not two facts. Only format-level reword is caught this way; annotated,
     confirmed, and superseded bullets are distinct text and are preserved, so the audit trail (#5) is
     never lost.

  2. **Re-tier via the registry.** Reconcile each inline ``[doc:ref·X]`` / ``[web:ref·X]`` tag against
     the *authoritative* tier the sources registry holds for that ref (``lookup_source``). When a source
     is re-graded — or the runtime tagged a bullet with the wrong tier — the consolidator corrects the
     inline tag to match. The registry is the single source of authority for a source's standing.

  3. **Reconcile backlinks (#9).** Delegates to :func:`memory.backlinks.reconcile` — a deterministic
     double-entry check that every recorded link still has both its markers and hasn't drifted. No LLM
     judgment; see ``memory/backlinks.py`` for why.

All three passes are **non-destructive** (no fact is deleted; supersession is recorded, not erased) and
**idempotent** (running twice produces no further change). Re-running yields an empty report.

A safety-ordered remediation ladder
------------------------------------
A bare "oversized section → compress" rule can't distinguish two different problems: duplicate bloat (safe
to collapse, loses nothing) and genuine drill-down growth (compressing it is real data loss). The fix is
ordering, not a smarter compressor: :func:`remediate_section` is a *triggered*, in-session counterpart to
the scheduled pass above — it fires the moment a single section crosses a budget threshold, rather than
waiting for the next nightly run, and tries ``exact_dedup`` (free, lossless, the same normalized-key match
:func:`consolidate` already runs) first, escalating to ``compress`` (illustrative-only here; production's
real prompt is withheld calibration) only while the section is still over budget after that. A section
that survives both rungs un-healed is flagged, not force-compressed — the ordering *is* the
cause-diagnosis: cheap and reversible before expensive and lossy.

Described-only (not in this runnable core): task archival, profile dedup, topic-reorg overflow handling
(including the corresponding backlink-marker rewrite on reorg), the production LLM-judgment merge prompts,
and the triggered ladder's real production calibration — the actual embedding-token budget threshold and
the per-topic cooldown between triggered runs. See docs/architecture.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from evidence.sources import SourcesRegistry
from memory.backlinks import Link, LinkIssue, reconcile as reconcile_links
from memory.store import MemoryStore

_H2_LINE_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_LINE_RE = re.compile(r"^\s*[-*]\s+")
# [doc:ref·tier] / [web:ref·tier] — capture the parts so we can rewrite only the tier.
_DOCWEB_TAG_RE = re.compile(r"\[(doc|web):([^\]·]+)·([A-G])\]")
# Runs of punctuation/symbol characters collapse to a single space for dedup-key purposes, so
# "tailwindcss: ^4.3.2" and "tailwindcss@^4.3.2" normalize to the same key.
_PUNCT_RUN_RE = re.compile(r"[^\w\s]+")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
# Demo-scaled illustrative length threshold for the triggered ladder — production's real embedding-token
# budget is withheld calibration (see module docstring).
_DEMO_SECTION_BUDGET_CHARS = 260


@dataclass
class ConsolidationReport:
    """What one consolidation pass changed. Empty across the board == corpus already coherent."""

    deduped: list[str] = field(default_factory=list)            # bullet text removed as duplicate
    retiered: list[tuple[str, str, str]] = field(default_factory=list)  # (ref, old_tier, new_tier)
    link_issues: list[LinkIssue] = field(default_factory=list)   # #9 — backlink reconciliation findings

    @property
    def changed(self) -> bool:
        return bool(self.deduped or self.retiered)


def consolidate(
    store: MemoryStore,
    registry: SourcesRegistry,
    links: list[Link] | None = None,
) -> ConsolidationReport:
    """One non-destructive, idempotent dedup + re-tier + link-reconciliation pass over the corpus.

    Rewrites the markdown source of truth in place, then rebuilds the derived FTS5 index so subsequent
    recall reflects the reconciled tiers. ``links`` is optional (omit if decision #9 isn't in play) —
    reconciliation never mutates the corpus, it only reports findings (``report.link_issues``).
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

    if links:
        report.link_issues = reconcile_links(store, links)

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


def _normalized_fact_key(line: str) -> str:
    """Punctuation-insensitive dedup key: collapse every run of punctuation/symbol characters to a single
    space. Catches format-only reword of the same fact (``tailwindcss: ^4.3.2`` vs
    ``tailwindcss@^4.3.2``) — the dominant duplicate pattern observed in practice — without touching
    genuine paraphrase, which this deliberately doesn't and shouldn't catch."""
    collapsed = _PUNCT_RUN_RE.sub(" ", line.strip())
    return _WHITESPACE_RUN_RE.sub(" ", collapsed).strip()


def _dedup_sections(markdown: str, report: ConsolidationReport) -> str:
    """Drop duplicate bullet lines (by normalized key) within each H2 section, preserving first
    occurrence + order."""
    out: list[str] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        if _H2_LINE_RE.match(line):
            seen = set()  # duplicates are scoped to a section; reset at each heading
            out.append(line)
            continue
        if _BULLET_LINE_RE.match(line):
            key = _normalized_fact_key(line)
            if key in seen:
                report.deduped.append(line.strip())
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")


@dataclass
class RemediationReport:
    """What one triggered remediation pass on a single section did. Rungs run in order — free/lossless
    before paid/lossy — and escalate only while the section is still over budget.

    ``healed`` reflects the actual outcome (is the section back under budget), not just whether a rung
    ran — ``compress_ran=True`` on a single-bullet section is a documented no-op (nothing left to
    compress), and that must not read as "healed" any more than doing nothing would."""

    section: str
    exact_dedup_removed: list[str] = field(default_factory=list)
    compress_ran: bool = False
    healed: bool = False


def _replace_section_body(markdown: str, heading: str, new_body: str) -> str:
    """Replace one H2 section's body, leaving its heading and every other section untouched."""
    pattern = re.compile(rf"(^##\s+{re.escape(heading)}\s*$\n)((?:.*\n?)*?)(?=^## |\Z)", re.MULTILINE)
    m = pattern.search(markdown)
    if not m:
        raise ValueError(f"section {heading!r} not found")
    return markdown[: m.start(2)] + new_body.rstrip("\n") + "\n" + markdown[m.end(2):]


def _illustrative_compress(body: str) -> str:
    """Minimal, illustrative stand-in for production's LLM-based, preservation-intended compress rung —
    collapses the section's bullets to one summarizing line noting how many facts it replaces. Production
    weighs which facts to keep/merge with an actual model call; that prompt is withheld calibration (see
    module docstring), and this crude stand-in makes no attempt to approximate it."""
    lines = [ln.strip() for ln in body.splitlines() if _BULLET_LINE_RE.match(ln)]
    if len(lines) <= 1:
        return body
    return f"- ({len(lines)} facts compressed — illustrative stand-in for production's compress rung)"


def remediate_section(
    store: MemoryStore,
    topic: str,
    heading: str,
    *,
    budget_chars: int = _DEMO_SECTION_BUDGET_CHARS,
) -> RemediationReport:
    """Triggered, in-session remediation for one section that's crossed a budget threshold — the
    counterpart to :func:`consolidate`'s scheduled pass, fired on demand rather than waiting for the
    nightly run. Tries ``exact_dedup`` first (free, lossless); escalates to ``compress`` only if the
    section is still over budget after that. A section that survives both rungs un-healed is left as-is,
    not force-compressed — see module docstring for why the ordering itself is the point.
    """
    section = store.get_section(topic, heading)
    if section is None:
        raise ValueError(f"{topic!r} has no section {heading!r}")

    report = RemediationReport(section=f"{topic}#{heading}")
    body = section.body

    if len(body) > budget_chars:
        lines = body.splitlines()
        seen: set[str] = set()
        kept: list[str] = []
        for line in lines:
            if _BULLET_LINE_RE.match(line):
                key = _normalized_fact_key(line)
                if key in seen:
                    report.exact_dedup_removed.append(line.strip())
                    continue
                seen.add(key)
            kept.append(line)
        body = "\n".join(kept)

    if len(body) > budget_chars:
        body = _illustrative_compress(body)
        report.compress_ran = True

    report.healed = len(section.body) > budget_chars and len(body) <= budget_chars

    if body != section.body:
        text = store.read_topic(topic)
        store.write_topic(topic, _replace_section_body(text, heading, body))
        store.rebuild_index()

    return report
