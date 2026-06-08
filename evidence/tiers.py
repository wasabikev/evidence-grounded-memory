"""Authority tier vocabulary and resolver (design decision #3).

The 7-tier / 14-category provenance model. A *tier* (A-G) expresses how much to trust a fact; a
*category* names the kind of source within reach of a tier. Every fact stored in ``memory/`` carries an
inline tag whose tier drives conflict resolution and verification flagging.

Publication boundary
--------------------
The tier/category *skeleton* below is published. The domain-calibrated **recognition cues** that
classify a raw source into one of these categories (the bit tuned over real engagements) are the moat
and are intentionally absent — this module maps a *known* category to its tier; it does not infer the
category from raw text. See docs/architecture.md#publication-boundary.
"""

from __future__ import annotations

# Tiers, strongest (A) to weakest (G). Order is significant: index == trust rank.
AUTHORITY_TIERS: tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G")

# One-line gloss per tier (illustrative, demo domain = home renovation / permitting).
TIER_LABELS: dict[str, str] = {
    "A": "Authoritative regulation (e.g. adopted building code)",
    "B": "Official guidance (e.g. permit office)",
    "C": "Professional standard / licensed expert",
    "D": "First-party document (e.g. signed contractor quote)",
    "E": "Aggregated third-party (e.g. product reviews)",
    "F": "User statement",
    "G": "Model inference",
}

# The published 14-category skeleton: category -> tier. The *recognition cues* that would map raw
# source text onto one of these categories are withheld (the moat); only the mapping ships.
CATEGORY_TIERS: dict[str, str] = {
    # A — authoritative regulation
    "regulation": "A",
    "statute": "A",
    # B — official guidance
    "official_guidance": "B",
    "agency_publication": "B",
    # C — professional standard / licensed expert
    "professional_standard": "C",
    "licensed_expert": "C",
    # D — first-party documents
    "first_party_document": "D",
    "contract": "D",
    # E — aggregated third-party
    "aggregated_reviews": "E",
    "vendor_material": "E",
    "reference_media": "E",
    # F — user
    "user_statement": "F",
    "user_document": "F",
    # G — model
    "model_inference": "G",
}

# Tiers a fact enters at *provisionally*: recorded, not yet trusted. Temporal re-grading (#5) promotes
# or supersedes these as stronger evidence arrives.
PROVISIONAL_TIERS: frozenset[str] = frozenset({"F", "G"})


def resolve_tier(category: str) -> str:
    """Map a source category to its authority tier (``"A"`` strongest … ``"G"`` weakest).

    Raises ``ValueError`` for an unknown category — the published skeleton is closed; classifying a raw
    source *into* a category is the withheld step, not this lookup.
    """
    try:
        return CATEGORY_TIERS[category]
    except KeyError as exc:
        raise ValueError(f"unknown source category: {category!r}") from exc


def tier_rank(tier: str) -> int:
    """Numeric trust rank for a tier — ``0`` (A, strongest) … ``6`` (G, weakest). Lower is stronger."""
    try:
        return AUTHORITY_TIERS.index(tier)
    except ValueError as exc:
        raise ValueError(f"unknown tier: {tier!r}") from exc


def is_stronger(a: str, b: str) -> bool:
    """True if tier ``a`` outranks tier ``b`` (A outranks B outranks … outranks G)."""
    return tier_rank(a) < tier_rank(b)


def is_provisional(tier: str) -> bool:
    """True for tiers that enter the store provisionally (F user, G model) and are subject to re-grading."""
    return tier in PROVISIONAL_TIERS
