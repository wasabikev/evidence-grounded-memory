"""Split-budget context injection + dual-path merge — THE CENTERPIECE (decisions #1 merge, #2).

This module carries the strongest single signal of the system's design. Two ideas combine here:

  * **Dual-path merge (#1).** Keyword results (``MemoryStore.search_sections``) and semantic results
    (``SemanticIndex.search``) are reconciled into one ranked set. Neither path alone suffices — exact
    technical terms come from keyword, conceptual proximity from semantic — so the *merge* is the point.
    We fuse with **Reciprocal Rank Fusion (RRF)**, which combines the two *rankings* (not raw scores, so
    no cross-path score calibration is needed). Production uses tuned score-fusion weights; RRF is the
    clean, re-derivable default published here (the weights are the withheld moat).

  * **Split budget (#2).** The assembled context is partitioned — a shared pool for the top section
    snippets and a *separate* INDEX cap for a compressed table-of-contents — so no single topic can
    crowd out cross-domain context. The token allocations are illustrative, re-derivable defaults, not
    tuned production values.

Section-level granularity (#7) is what makes the budget meaningful: we pack *sections*, not whole topics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from memory.semantic_index import SemanticIndex
from memory.store import MemoryStore, Section, SectionMatch

# RRF constant. The standard k0=60 from Cormack et al.; dampens the weight of top ranks so a section
# need not win *both* paths to surface. Re-derivable default, not tuned.
_RRF_K0 = 60

# Per-path admission thresholds applied *before* fusion. Illustrative — they exist to drop the weak tail
# of each path, not as calibrated production values.
_KEYWORD_MIN_RELEVANCE = 0.0   # bm25-derived; >0 means the section matched at least one query term
_SEMANTIC_MIN_RELEVANCE = 0.05  # cosine; below this the section is conceptually unrelated noise


@dataclass(frozen=True)
class MemoryBudget:
    """Illustrative split budget. Defaults are re-derivable, not production-tuned."""

    shared_pool_tokens: int = 2000   # top section snippets, dual-path merged
    index_cap_tokens: int = 2100     # compressed table-of-contents, separate cap


@dataclass
class MergedHit:
    """One section after dual-path fusion, with the provenance of *how* it was retrieved."""

    section: Section
    fused_score: float
    paths: set[str] = field(default_factory=set)   # {"keyword"}, {"semantic"}, or both
    keyword_rank: int | None = None
    semantic_rank: int | None = None


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 chars/token). Good enough for budget packing."""
    return max(1, len(text) // 4)


def merge_dual_path(
    keyword: list[SectionMatch], semantic: list[SectionMatch]
) -> list[MergedHit]:
    """Reconcile keyword and semantic results into one ranked set via Reciprocal Rank Fusion.

    A section retrieved by *both* paths is rewarded (its RRF contributions sum); a section strong in only
    one path still surfaces. This is the merge the whole dual-path design exists for.
    """
    hits: dict[str, MergedHit] = {}

    def admit(matches: list[SectionMatch], path: str, min_relevance: float) -> None:
        rank = 0
        for m in matches:
            if m.relevance < min_relevance:
                continue
            rank += 1  # 1-based rank within this path (after thresholding)
            hit = hits.get(m.section.id)
            if hit is None:
                hit = MergedHit(section=m.section, fused_score=0.0)
                hits[m.section.id] = hit
            hit.fused_score += 1.0 / (_RRF_K0 + rank)
            hit.paths.add(path)
            if path == "keyword":
                hit.keyword_rank = rank
            else:
                hit.semantic_rank = rank

    admit(keyword, "keyword", _KEYWORD_MIN_RELEVANCE)
    admit(semantic, "semantic", _SEMANTIC_MIN_RELEVANCE)

    merged = list(hits.values())
    # Sort by fused score; break ties toward sections both paths agreed on, then by id for determinism.
    merged.sort(key=lambda h: (h.fused_score, len(h.paths), h.section.id), reverse=True)
    return merged


def recall(
    query: str,
    store: MemoryStore,
    semantic: SemanticIndex,
    *,
    per_path_limit: int = 8,
) -> list[MergedHit]:
    """Run both retrieval paths and return the merged, ranked hits (the testable core of injection)."""
    keyword = store.search_sections(query, limit=per_path_limit)
    semantic_hits = semantic.search(query, limit=per_path_limit)
    return merge_dual_path(keyword, semantic_hits)


def _render_snippet(section: Section) -> str:
    return f"### {section.topic} — {section.heading}\n{section.body}"


def _build_index_block(store: MemoryStore, budget: MemoryBudget) -> tuple[str, int]:
    """Compressed table-of-contents: one line per topic listing its section headings.

    Lives under its *own* cap so the at-a-glance map of what memory knows is never starved by snippet
    packing (decision #2 — the partition, not just a budget).
    """
    lines: list[str] = []
    used = 0
    headings_by_topic: dict[str, list[str]] = {}
    for sec in store.iter_sections():
        headings_by_topic.setdefault(sec.topic, []).append(sec.heading)
    for topic, headings in headings_by_topic.items():
        line = f"- {topic}: {', '.join(headings)}"
        cost = estimate_tokens(line)
        if used + cost > budget.index_cap_tokens:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines), used


def inject(
    query: str,
    store: MemoryStore,
    semantic: SemanticIndex,
    budget: MemoryBudget = MemoryBudget(),
) -> str:
    """Assemble the passive memory context for one model call.

    Runs both retrieval paths, merges them (#1), and packs the relevant *sections* (#7) into the split
    budget (#2): a shared snippet pool plus a separately-capped INDEX. Returns the context block a model
    would receive.
    """
    merged = recall(query, store, semantic)

    # --- shared pool: greedily pack the best merged sections under the shared cap --------------------
    packed: list[MergedHit] = []
    used = 0
    for hit in merged:
        cost = estimate_tokens(_render_snippet(hit.section))
        if used + cost > budget.shared_pool_tokens:
            continue  # skip and keep trying smaller later sections rather than stopping outright
        packed.append(hit)
        used += cost

    # --- INDEX block: a compressed map of *all* topics, under its own separate cap -------------------
    index_block, _ = _build_index_block(store, budget)

    # --- assemble ----------------------------------------------------------------------------------
    out: list[str] = [f"# MEMORY (injected for query: {query!r})", ""]
    out.append(f"## Recalled sections  [shared pool ≤ {budget.shared_pool_tokens} tok]")
    if packed:
        for hit in packed:
            via = "+".join(sorted(hit.paths))
            out.append(f"<!-- via {via}; fused={hit.fused_score:.4f} -->")
            out.append(_render_snippet(hit.section))
            out.append("")
    else:
        out.append("_(no sections matched)_\n")
    out.append(f"## INDEX — topics known  [separate cap ≤ {budget.index_cap_tokens} tok]")
    out.append(index_block)
    return "\n".join(out).rstrip() + "\n"
