"""Passive document inventory — the discovery half of document recall (decision #8).

The agent is *told what source documents exist* every turn, so discovery is a structural property of the
context rather than a tool call the agent must remember to make. This is the genuinely new idea in #8
(alongside treating documents as a first-class substrate): the knowledge repository surfaces its own
primary sources.

Selection mirrors production: a small **recent ∪ query-matched** set under a dedicated cap, with strong
matches flagged ``possibly related`` and the long tail collapsed behind a ``+N more`` line. Two production
choices are shown here as *concepts with the calibration withheld*:

  * a **display gist** (truncated) distinct from the full gist that the index searches; and
  * a **multi-token overlap gate** — require ≥N distinct query terms to co-occur in a document before
    flagging it ``possibly related``, so one weak token can't flood the inventory. The threshold/weighting
    are production-tuned; the value here is an illustrative default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from documents.index import Document, DocumentIndex

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.]*")
# Words too common to count as meaningful query/document overlap. Illustrative, not a tuned stoplist.
_STOPWORDS = frozenset(
    "a an and are as at be by for from how in is it of on or over the this to vs with what when".split()
)
# Concept knob (production-tuned value withheld): distinct query terms that must co-occur in a document
# before it is called "possibly related" in the passive block.
_MIN_QUERY_OVERLAP = 2
# Display gist is truncated; the full gist still lives in the index and is what search matches on.
_GIST_DISPLAY_CHARS = 110


def _content_terms(text: str) -> set[str]:
    return {t.lower() for t in _WORD_RE.findall(text)} - _STOPWORDS


def _short_gist(gist: str) -> str:
    g = " ".join(gist.split())
    return g if len(g) <= _GIST_DISPLAY_CHARS else g[: _GIST_DISPLAY_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class InventoryRow:
    document: Document
    possibly_related: bool


def select_inventory(
    index: DocumentIndex,
    query: str | None = None,
    *,
    recent_cap: int = 10,
) -> tuple[list[InventoryRow], int]:
    """Choose the documents to surface this turn: recent ∪ query-matched, matched first.

    Returns ``(rows, overflow)`` where ``overflow`` is the count collapsed behind "+N more". A query-matched
    document is included *even if it falls outside the recent window* — the long-tail rescue that stops an
    older-but-relevant source from going invisible.
    """
    all_docs = index.documents()  # most-recent first

    matched_refs: set[str] = set()
    if query:
        q_terms = _content_terms(query)
        for m in index.search(query):
            doc = m.document
            doc_terms = _content_terms(f"{doc.filename} {doc.gist} {doc.body}")
            if len(q_terms & doc_terms) >= _MIN_QUERY_OVERLAP:
                matched_refs.add(doc.ref)

    recent = all_docs[:recent_cap]
    recent_refs = {d.ref for d in recent}
    extra_matched = [d for d in all_docs if d.ref in matched_refs and d.ref not in recent_refs]

    selected = extra_matched + recent  # matched-but-out-of-window first, then recent
    rows = [InventoryRow(d, d.ref in matched_refs) for d in selected]
    # Stable sort: possibly-related first, original (recency) order preserved within each group.
    rows.sort(key=lambda r: (not r.possibly_related,))

    overflow = max(0, len(all_docs) - len(selected))
    return rows, overflow


def render_inventory(index: DocumentIndex, query: str | None = None, *, recent_cap: int = 10) -> str:
    """Render the passive 'Source Documents' block the agent receives every turn."""
    rows, overflow = select_inventory(index, query, recent_cap=recent_cap)
    out = ["## Source documents on file"]
    if not rows:
        out.append("_(none on file)_")
        return "\n".join(out)
    for r in rows:
        flag = "  **(possibly related)**" if r.possibly_related else ""
        out.append(f"- [doc:{r.document.ref}] {r.document.filename} — {_short_gist(r.document.gist)}{flag}")
    if overflow:
        out.append(f"- +{overflow} more document{'s' if overflow != 1 else ''} on file")
    return "\n".join(out)
