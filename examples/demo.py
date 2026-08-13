"""End-to-end demo: ingest -> tag -> recall -> inject -> re-grade -> consolidate.

Domain: home renovation / permitting (spans all 7 authority tiers). The runnable proof that the pieces
compose. It walks one scenario through the full pipeline:

  1. Ingest the committed corpus (``examples/corpus``) — markdown topic files with inline provenance tags
     spanning building code (A), permit guidance (B), a professional standard (C), a contractor quote
     (D), aggregated reviews (E), homeowner statements (F), and an AI inference (G).
  2. Build the derived indexes (FTS5 keyword + semantic vectors) over the markdown source of truth.
  3. Recall: run dual-path retrieval for a query and inject the split-budget context block.
  4. Re-grade (synchronous, #5): promote a provisional homeowner statement when official guidance
     corroborates it, and supersede an AI inference when the contractor quote contradicts it — both
     recorded non-destructively in the markdown.
  5. Consolidate (asynchronous, #6): the scheduled backstop dedups a duplicate bullet and re-tiers an
     inline tag the runtime got wrong, reconciling against the sources registry.
  6. Cross-topic backlinks (#9): record a bidirectional link between two topics a vocabulary-mismatched
     query would never connect, reconcile it (clean), then show reconciliation catching genuine drift
     after one side's content actually changes.

Run:  python examples/demo.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# Make the repo root importable when run as `python examples/demo.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Provenance tags carry the middle dot (·) and the output uses ≤; force UTF-8 so the demo prints on
# consoles whose default encoding (e.g. Windows cp1252) can't represent them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from documents.index import DocumentIndex  # noqa: E402
from evidence.sources import SourcesRegistry  # noqa: E402
from memory.backlinks import LinksRegistry, render_link  # noqa: E402
from memory.consolidator import consolidate  # noqa: E402
from memory.injector import MemoryBudget, inject, recall  # noqa: E402
from memory.semantic_index import SemanticIndex  # noqa: E402
from memory.store import MemoryStore  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus"


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _setup_workspace() -> Path:
    """Copy the committed corpus into a throwaway workspace so re-grading can mutate it freely."""
    ws = Path(tempfile.mkdtemp(prefix="egm_demo_"))
    shutil.copytree(CORPUS / "topics", ws / "topics")
    shutil.copy(CORPUS / "sources.jsonl", ws / "sources.jsonl")
    shutil.copy(CORPUS / "documents.jsonl", ws / "documents.jsonl")
    shutil.copytree(CORPUS / "documents", ws / "documents")
    return ws


def _build(ws: Path) -> tuple[MemoryStore, SemanticIndex, SourcesRegistry]:
    store = MemoryStore(ws)
    store.rebuild_index()
    semantic = SemanticIndex()
    semantic.rebuild(store.iter_sections())
    registry = SourcesRegistry(ws / "sources.jsonl")
    return store, semantic, registry


def main() -> None:
    ws = _setup_workspace()
    print(f"workspace: {ws}")
    store, semantic, registry = _build(ws)

    # --- 1. dual-path recall + split-budget injection (#1, #2, #7) --------------------------------
    _rule("1. DUAL-PATH RECALL + SPLIT-BUDGET INJECTION")
    query = "permit needed for a new deck circuit (code R105.2)"
    print(f"query: {query!r}\n")

    print("merged hits (which path retrieved each section):")
    for hit in recall(query, store, semantic):
        via = "+".join(sorted(hit.paths))
        print(f"  {hit.fused_score:.4f}  [{via:16}]  {hit.section.id}")

    print("\ninjected context block (what the model would receive):\n")
    print(inject(query, store, semantic, MemoryBudget()))

    # --- 2. document recall: on-demand content search (#8) ----------------------------------------
    _rule("2. DOCUMENT RECALL (on-demand content search)")
    doc_index = DocumentIndex.from_manifest(ws / "documents.jsonl")

    money_q = "is cedar or composite cheaper over ten years for this deck"
    print(f"query: {money_q!r}\n")

    # The production failure this fixes was filename-only matching. The quote that holds the answer is
    # named "Quote #4471" — its filename contains none of the query's content terms, so a name search
    # misses it entirely. (This is the home-renovation analog of a confirmation PDF a name search can't find.)
    needles = ["cedar", "composite", "ten-year", "ten year", "cost"]
    filename_hits = [d for d in doc_index.documents() if any(n in d.filename.lower() for n in needles)]
    print(f"filename-only search (the old behavior) -> {len(filename_hits)} hits")

    # Content-aware search over filename + gist + body finds it — the same standard FTS5 retrieval as the
    # keyword path (#1), applied to the document substrate. The decision is the substrate + its surfacing.
    content_hits = doc_index.search(money_q)
    print("content search (filename + gist + body):")
    for m in content_hits:
        print(f"  {m.relevance:7.4f}  [doc:{m.document.ref}]  {m.document.filename}")

    discovered = content_hits[0].document
    print(f"\n-> discovered source: [doc:{discovered.ref}] {discovered.filename}")
    print("   the agent reaches for this explicitly, the way search_documents works in production —")
    print("   and this is the same source that drives the supersession in the next act.")

    # --- 3. temporal re-grading, synchronous path (#5) -------------------------------------------
    _rule("3. TEMPORAL RE-GRADING (synchronous, in-conversation)")

    print("BEFORE — permits / Deck permits:\n")
    print(store.get_section("permits", "Deck permits").body)

    # Promotion: the homeowner's provisional 'two weeks' estimate (F) is corroborated by the permit
    # office guidance (B). Effective trust rises; history is annotated, not rewritten.
    store.confirm_bullet("permits", "Deck permits", "about two weeks", ref="e041")

    print("\nAFTER promotion (homeowner estimate corroborated by official guidance e041):\n")
    print(store.get_section("permits", "Deck permits").body)

    print("\nBEFORE — materials / Decking boards:\n")
    print(store.get_section("materials", "Decking boards").body)

    # Supersession: the AI inference (G) that composite is cheaper is contradicted by the actual
    # contractor quote (D) — the very document surfaced by the discovery act above. Recorded
    # bidirectionally and non-destructively. The ref comes from discovery, not a hardcode: #8 feeds #5.
    store.supersede_bullet(
        "materials",
        "Decking boards",
        old_needle="composite likely has the lower ten-year cost",
        new_bullet=(
            f"Using Northwood's actual cedar quote, cedar has the lower ten-year cost for this "
            f"project. [doc:{discovered.ref}·D]"
        ),
        ref=discovered.ref,
        date="2026-06-05",
    )
    store.rebuild_index()
    semantic.rebuild(store.iter_sections())

    print("\nAFTER supersession (AI inference contradicted by contractor quote e060 — original kept):\n")
    print(store.get_section("materials", "Decking boards").body)

    # --- 4. consolidation, asynchronous backstop (#6) --------------------------------------------
    _rule("4. CONSOLIDATION (asynchronous backstop: dedup + re-tier)")

    # Simulate two things the runtime left behind:
    #  (a) a duplicate bullet written by a second extraction pass, and
    #  (b) a bullet the runtime mis-tagged ·C when the registry grades e041 as ·B.
    elec = store.read_topic("permits")
    elec = elec.replace(
        "- The inspector wants the panel schedule labeled before the rough-in inspection. [doc:e041·B]\n",
        "- The inspector wants the panel schedule labeled before the rough-in inspection. [doc:e041·B]\n"
        "- The inspector wants the panel schedule labeled before the rough-in inspection. [doc:e041·B]\n"
        "- Service upgrades over 200 amps need a separate utility coordination step. [doc:e041·C]\n",
    )
    store.write_topic("permits", elec)
    store.rebuild_index()

    print("BEFORE — permits / Electrical permits (note duplicate + mis-tiered ·C):\n")
    print(store.get_section("permits", "Electrical permits").body)

    report = consolidate(store, registry)

    print("\nconsolidation report:")
    print(f"  deduped : {report.deduped}")
    print(f"  retiered: {report.retiered}")

    print("\nAFTER consolidation:\n")
    print(store.get_section("permits", "Electrical permits").body)

    # Idempotence: a second pass finds nothing left to do.
    second = consolidate(store, registry)
    print(f"\nsecond pass changed anything? {second.changed}  (expected: False)")

    # --- 6. cross-topic backlinks (#9) -------------------------------------------------------------
    _rule("6. CROSS-TOPIC BACKLINKS")

    links = LinksRegistry(ws / "backlinks.jsonl")

    # The detection step — "does this content also relate to another topic" — is an LLM judgment call
    # in production, piggybacked on the same extraction call that decides routing. This repo has no LLM
    # at runtime, so the decision is baked in here as a labeled fixture: real design, illustrative data.
    # The link: a homeowner choosing decking material won't think to ask about permits, but the permit
    # rule is exactly what the material choice runs into.
    print("fixture decision (stands in for the production LLM judgment call):")
    print('  materials / Decking boards  <->  permits / Deck permits\n')

    print("BEFORE — materials / Decking boards:\n")
    print(store.get_section("materials", "Decking boards").body)
    print("\nBEFORE — permits / Deck permits:\n")
    print(store.get_section("permits", "Deck permits").body)

    link = render_link(store, "materials", "Decking boards", "permits", "Deck permits")
    links.upsert(link)
    store.rebuild_index()
    semantic.rebuild(store.iter_sections())

    print("\nAFTER rendering the link — both sides get an independently-recorded marker:\n")
    print("materials / Decking boards:\n" + store.get_section("materials", "Decking boards").body)
    print("\npermits / Deck permits:\n" + store.get_section("permits", "Deck permits").body)

    report = consolidate(store, registry, links=links.all())
    print(f"\nreconciliation right after linking: {report.link_issues}  (expected: [])")

    # Genuine drift: the permit's structural-review trigger gets a real update — not a marker churn,
    # an actual new fact. A purely *derived* backlink (recomputed from one side, never independently
    # recorded) could never detect this; the double-entry framing is what makes it checkable.
    permits_text = store.read_topic("permits")
    permits_text = permits_text.replace(
        "- Decks over 200 square feet also require a structural plan review. [doc:e037·A]\n",
        "- Decks over 200 square feet also require a structural plan review. [doc:e037·A]\n"
        "- As of the 2026 code update, structural plan review now also applies to decks over 8 feet"
        " above grade, regardless of square footage. [doc:e037·A]\n",
    )
    store.write_topic("permits", permits_text)
    store.rebuild_index()

    report = consolidate(store, registry, links=links.all())
    print(f"reconciliation after permits/Deck permits genuinely changed: {report.link_issues}")
    print("(expected: one 'stale' finding — the link survives a re-render; production's nightly")
    print(" consolidator surfaces this as a flag for review, it doesn't auto-resolve it)")

    _rule("DONE")
    print(f"inspect the mutated workspace at: {ws}")


if __name__ == "__main__":
    main()
