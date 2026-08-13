"""Behavior tests for the retrieval core. Each test is an executable claim about a design decision.

  - dual-path recall: keyword catches an exact term semantic ranks low; semantic catches a morphological
    match keyword misses entirely (#1)
  - split-budget injection: a tiny shared pool can't starve the separately-capped INDEX (#2)
  - section-level indexing: a query injects one section, not the whole topic (#7)
  - temporal re-grading: promotion and bidirectional supersession, both non-destructive (#5)
  - consolidation: punctuation-insensitive dedup + re-tier, idempotent (#6)
  - triggered remediation ladder: exact_dedup before compress, escalating only while still over budget,
    a section under budget left untouched (#6)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence.sources import SourcesRegistry, source_from_category  # noqa: E402
from memory.consolidator import consolidate, remediate_section  # noqa: E402
from memory.injector import MemoryBudget, inject, recall  # noqa: E402
from memory.semantic_index import SemanticIndex  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


def make_store(topics: dict[str, str]) -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="egm_test_"))
    store = MemoryStore(tmp)
    for name, md in topics.items():
        store.write_topic(name, md)
    store.rebuild_index()
    return store


def make_semantic(store: MemoryStore) -> SemanticIndex:
    sem = SemanticIndex()
    sem.rebuild(store.iter_sections())
    return sem


class DualPathRecallTests(unittest.TestCase):
    """#1 — neither path alone is enough; the merge is the point."""

    def setUp(self):
        self.store = make_store(
            {
                "guide": (
                    "# Guide\n\n"
                    "## Exempt repairs\n"
                    "- Permit code R105.2 lists the minor repairs that are exempt.\n\n"
                    "## Application process\n"
                    "- The permit workflow for a deck involves submitting plans.\n"
                ),
            }
        )
        self.sem = make_semantic(self.store)

    def test_keyword_finds_exact_code(self):
        ids = [m.section.id for m in self.store.search_sections("R105.2")]
        self.assertIn("guide#Exempt repairs", ids)

    def test_keyword_misses_morphological_variant(self):
        # "permitting" is not a token anywhere in the corpus ("permit" is), so exact keyword search
        # returns nothing — the gap the semantic path exists to cover.
        self.assertEqual(self.store.search_sections("permitting"), [])

    def test_semantic_recovers_what_keyword_missed(self):
        ids = [m.section.id for m in self.sem.search("permitting")]
        self.assertIn("guide#Application process", ids)

    def test_merge_unions_both_paths(self):
        hits = recall("permitting R105.2", self.store, self.sem)
        ids = {h.section.id for h in hits}
        self.assertIn("guide#Exempt repairs", ids)       # via keyword (exact code)
        self.assertIn("guide#Application process", ids)   # via semantic (morphological)


class SectionLevelInjectionTests(unittest.TestCase):
    """#7 — inject the relevant section, not the whole topic."""

    def test_one_section_indexed_not_the_document(self):
        store = make_store(
            {
                "topic": (
                    "# Topic\n\n"
                    "## Alpha\n- alpha apple apricot\n\n"
                    "## Beta\n- beta banana blueberry\n"
                ),
            }
        )
        results = store.search_sections("banana")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].section.heading, "Beta")
        self.assertNotIn("apple", results[0].section.body)


class SplitBudgetTests(unittest.TestCase):
    """#2 — a partitioned budget keeps the cross-domain INDEX alive under snippet pressure."""

    def setUp(self):
        # Three topics; one section is large enough to dominate a single shared budget.
        big = "## Big\n" + "\n".join(f"- detail line number {i} about decks and permits" for i in range(40))
        self.store = make_store(
            {
                "alpha": f"# Alpha\n\n{big}\n",
                "beta": "# Beta\n\n## Beta\n- a short note about wiring\n",
                "gamma": "# Gamma\n\n## Gamma\n- a short note about fasteners\n",
            }
        )
        self.sem = make_semantic(self.store)

    def test_large_topic_cannot_crowd_out_cross_domain_context(self):
        # `alpha` ranks highest (it repeats "decks and permits") but is far too large for the tiny
        # shared pool. The packer skips it and admits the small `beta`/`gamma` snippets instead — and
        # the separately-capped INDEX still names every topic. No single topic monopolizes the prompt.
        budget = MemoryBudget(shared_pool_tokens=30, index_cap_tokens=2000)
        out = inject("decks permits wiring fasteners", self.store, self.sem, budget)
        recalled, _, index = out.partition("## INDEX")
        self.assertNotIn("### alpha", recalled)              # the large top-ranked topic was skipped
        self.assertIn("### beta", recalled)                  # smaller cross-domain topics still got in
        self.assertIn("### gamma", recalled)
        for topic in ("alpha", "beta", "gamma"):
            self.assertIn(topic, index)                       # INDEX still maps every topic


class TemporalRegradingTests(unittest.TestCase):
    """#5 — promotion and bidirectional supersession, both non-destructive."""

    def setUp(self):
        self.store = make_store(
            {
                "topic": (
                    "# Topic\n\n"
                    "## Section\n"
                    "- review takes about two weeks. [user]\n"
                    "- composite is cheaper over ten years. [ai]\n"
                ),
            }
        )

    def test_promotion_annotates_without_rewriting(self):
        self.assertTrue(self.store.confirm_bullet("topic", "Section", "two weeks", ref="e041"))
        body = self.store.get_section("topic", "Section").body
        self.assertIn("two weeks. [user] (confirmed by e041)", body)

    def test_promotion_is_idempotent(self):
        self.store.confirm_bullet("topic", "Section", "two weeks", ref="e041")
        self.store.confirm_bullet("topic", "Section", "two weeks", ref="e041")
        body = self.store.get_section("topic", "Section").body
        self.assertEqual(body.count("(confirmed by e041)"), 1)

    def test_supersession_is_bidirectional_and_keeps_the_original(self):
        ok = self.store.supersede_bullet(
            "topic",
            "Section",
            old_needle="composite is cheaper",
            new_bullet="cedar is cheaper over ten years for this project. [doc:e060·D]",
            ref="e060",
            date="2026-06-05",
        )
        self.assertTrue(ok)
        body = self.store.get_section("topic", "Section").body
        # Old bullet is retained and marked; new bullet records what it supersedes.
        self.assertIn("composite is cheaper over ten years. [ai] (superseded 2026-06-05 by e060)", body)
        self.assertIn("[doc:e060·D] (supersedes:", body)
        self.assertIn("cedar is cheaper", body)


class ConsolidationTests(unittest.TestCase):
    """#6 — slim dedup + re-tier, non-destructive and idempotent."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="egm_cons_"))
        self.store = MemoryStore(self.tmp)
        self.store.write_topic(
            "topic",
            "# Topic\n\n"
            "## Section\n"
            "- the inspector wants the panel labeled. [doc:e041·B]\n"
            "- the inspector wants the panel labeled. [doc:e041·B]\n"   # exact duplicate
            "- service upgrades need utility coordination. [doc:e041·C]\n",  # mis-tiered (registry: B)
        )
        self.store.rebuild_index()
        self.registry = SourcesRegistry(self.tmp / "sources.jsonl")
        self.registry.register(source_from_category("e041", "Permit office guidance", "official_guidance"))

    def test_dedup_and_retier(self):
        report = consolidate(self.store, self.registry)
        self.assertEqual(len(report.deduped), 1)
        self.assertEqual(report.retiered, [("e041", "C", "B")])
        body = self.store.get_section("topic", "Section").body
        self.assertEqual(body.count("the inspector wants the panel labeled"), 1)
        self.assertIn("service upgrades need utility coordination. [doc:e041·B]", body)

    def test_idempotent(self):
        consolidate(self.store, self.registry)
        second = consolidate(self.store, self.registry)
        self.assertFalse(second.changed)

    def test_punctuation_only_variant_is_caught_as_a_duplicate(self):
        # Same fact, reworded punctuation only -- the dominant duplicate pattern observed in practice,
        # distinct from genuine paraphrase (which dedup deliberately doesn't touch).
        tmp = Path(tempfile.mkdtemp(prefix="egm_cons_punct_"))
        store = MemoryStore(tmp)
        store.write_topic(
            "topic",
            "# Topic\n\n"
            "## Section\n"
            "- tailwindcss: ^4.3.2\n"
            "- tailwindcss@^4.3.2\n",
        )
        store.rebuild_index()
        registry = SourcesRegistry(tmp / "sources.jsonl")
        report = consolidate(store, registry)
        self.assertEqual(len(report.deduped), 1)
        body = store.get_section("topic", "Section").body
        self.assertEqual(len(body.splitlines()), 1)

    def test_genuine_paraphrase_is_not_deduped(self):
        # Different wording of arguably-the-same idea is NOT punctuation-only rework -- dedup must not
        # touch it; that's #5's re-grading job, not #6's.
        tmp = Path(tempfile.mkdtemp(prefix="egm_cons_paraphrase_"))
        store = MemoryStore(tmp)
        store.write_topic(
            "topic",
            "# Topic\n\n"
            "## Section\n"
            "- cedar has the lower ten-year cost. [doc:e060·D]\n"
            "- composite is more expensive over ten years. [doc:e060·D]\n",
        )
        store.rebuild_index()
        registry = SourcesRegistry(tmp / "sources.jsonl")
        report = consolidate(store, registry)
        self.assertEqual(report.deduped, [])


class RemediationLadderTests(unittest.TestCase):
    """#6 (triggered) — an in-session ladder for one oversized section: exact_dedup before compress,
    escalating only while still over budget."""

    def _store_with_section(self, body_lines: list[str]) -> MemoryStore:
        tmp = Path(tempfile.mkdtemp(prefix="egm_remediate_"))
        store = MemoryStore(tmp)
        store.write_topic("topic", "# Topic\n\n## Section\n" + "\n".join(body_lines) + "\n")
        store.rebuild_index()
        return store

    def test_under_budget_section_is_left_untouched(self):
        store = self._store_with_section(["- a short fact. [user]"])
        report = remediate_section(store, "topic", "Section", budget_chars=200)
        self.assertFalse(report.healed)
        self.assertEqual(report.exact_dedup_removed, [])
        self.assertFalse(report.compress_ran)

    def test_exact_dedup_alone_heals_when_that_was_the_only_bloat(self):
        # Over budget only because of a punctuation-variant duplicate -- same words, different
        # punctuation -- rung 1 alone should heal it, never reaching the lossy rung 2.
        long_fact = "- fastener spec: grade 316 stainless required within 1500 ft of saltwater. [doc:e055·C]"
        variant = "- fastener spec — grade 316 stainless required within 1500 ft of saltwater. [doc:e055·C]"
        store = self._store_with_section([long_fact, variant])
        report = remediate_section(store, "topic", "Section", budget_chars=len(long_fact) + 20)
        self.assertEqual(len(report.exact_dedup_removed), 1)
        self.assertFalse(report.compress_ran)
        self.assertTrue(report.healed)
        body = store.get_section("topic", "Section").body
        self.assertEqual(body.count("grade 316 stainless"), 1)

    def test_escalates_to_compress_when_still_over_budget_after_dedup(self):
        store = self._store_with_section(
            [
                "- fastener spec: grade 316 stainless required within 1500 ft of saltwater. [doc:e055·C]",
                "- fastener spec — grade 316 stainless required within 1500 ft of saltwater. [doc:e055·C]",
                "- torque specs are listed in the manufacturer's install guide, not the building code."
                " [doc:e055·C]",
            ]
        )
        report = remediate_section(store, "topic", "Section", budget_chars=120)
        self.assertEqual(len(report.exact_dedup_removed), 1)
        self.assertTrue(report.compress_ran)
        self.assertTrue(report.healed)
        body = store.get_section("topic", "Section").body
        self.assertEqual(len(body.splitlines()), 1)
        self.assertIn("compressed", body)

    def test_survives_both_rungs_unhealed_is_left_as_is_not_force_compressed(self):
        # A single fact that's already over budget on its own -- neither rung can shrink it (nothing to
        # dedup, and the illustrative compress rung is a no-op on a single bullet), so it's left as-is.
        one_long_fact = "- " + ("x" * 300) + ". [user]"
        store = self._store_with_section([one_long_fact])
        report = remediate_section(store, "topic", "Section", budget_chars=50)
        self.assertFalse(report.healed)
        body = store.get_section("topic", "Section").body
        self.assertEqual(body, one_long_fact)


if __name__ == "__main__":
    unittest.main()
