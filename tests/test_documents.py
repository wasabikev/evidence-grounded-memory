"""Behavior tests for document recall (#8). Each test is an executable claim about the decision.

  - content recall: a query whose answer lives in a document *body* finds it, where filename-only
    matching (the production bug) misses — the core of #8
  - passive inventory: the agent is told what's on file; only a genuinely-related document is flagged
    "possibly related" (the multi-token overlap gate), and an older matched doc is rescued past the
    recency cap
  - identity: a document's ref is the same id the sources registry (#4) uses — not a separate id space

Runs against the committed demo corpus, so a fixture change that breaks the claim breaks the test.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from documents.index import Document, DocumentIndex  # noqa: E402
from documents.inventory import render_inventory, select_inventory  # noqa: E402
from evidence.sources import SourcesRegistry  # noqa: E402

CORPUS = Path(__file__).resolve().parent.parent / "examples" / "corpus"
MONEY_QUERY = "is cedar or composite cheaper over ten years for this deck"


class ContentRecallTests(unittest.TestCase):
    """#8 — content the filename can't reach becomes findable."""

    def setUp(self):
        self.index = DocumentIndex.from_manifest(CORPUS / "documents.jsonl")

    def test_filename_search_misses_the_answer(self):
        # The quote that holds the answer is named "Quote #4471" — none of the discriminating content
        # terms appear in its filename, so the old filename-only behavior finds nothing.
        quote = self.index.get("e060")
        self.assertIsNotNone(quote)
        for term in ("cedar", "composite", "ten-year", "cost"):
            self.assertNotIn(term, quote.filename.lower())

    def test_content_search_finds_what_the_filename_missed(self):
        hits = self.index.search(MONEY_QUERY)
        self.assertTrue(hits, "content search should return at least one document")
        self.assertEqual(hits[0].document.ref, "e060")

    def test_index_searches_the_body_not_just_metadata(self):
        # "resealing" appears only in the quote's body, never in any filename or gist.
        hits = self.index.search("resealing cycles cedar")
        self.assertIn("e060", {h.document.ref for h in hits})


class PassiveInventoryTests(unittest.TestCase):
    """#8 — discovery is structural: the agent is told what's on file, with relevant items flagged."""

    def setUp(self):
        self.index = DocumentIndex.from_manifest(CORPUS / "documents.jsonl")

    def test_inventory_lists_everything_on_file(self):
        block = render_inventory(self.index)
        for ref in ("e060", "e041", "e055"):
            self.assertIn(f"[doc:{ref}]", block)

    def test_only_the_relevant_document_is_flagged(self):
        rows, _ = select_inventory(self.index, MONEY_QUERY)
        flagged = {r.document.ref for r in rows if r.possibly_related}
        # The quote is relevant; the permit-guidance and fastener docs merely share the word "deck",
        # which the multi-token overlap gate is there to reject.
        self.assertEqual(flagged, {"e060"})

    def test_no_query_means_no_false_relevance(self):
        rows, _ = select_inventory(self.index, None)
        self.assertEqual(len(rows), 3)
        self.assertFalse(any(r.possibly_related for r in rows))


class InventorySelectionTests(unittest.TestCase):
    """#8 — the injected set is recent ∪ query-matched, so a relevant older doc is rescued past the cap."""

    def _index(self) -> DocumentIndex:
        docs = [
            Document("d1", "newest.pdf", "newest", "alpha", date="2026-05-05"),
            Document("d2", "newer.pdf", "newer", "bravo", date="2026-05-04"),
            Document("d3", "mid.pdf", "mid", "charlie", date="2026-05-03"),
            Document("d4", "older.pdf", "older", "delta", date="2026-05-02"),
            # The relevant document is the OLDEST — it would fall outside a small recency window.
            Document("d5", "oldest.pdf", "oldest", "stainless steel coastal fastener", date="2026-05-01"),
        ]
        return DocumentIndex(docs)

    def test_matched_old_doc_is_rescued_past_recency_cap(self):
        rows, overflow = select_inventory(self._index(), "stainless steel fastener", recent_cap=2)
        refs = {r.document.ref for r in rows}
        self.assertIn("d5", refs)  # rescued despite being oldest
        self.assertTrue(next(r for r in rows if r.document.ref == "d5").possibly_related)
        # recent(2) ∪ matched(1) = 3 surfaced; the two unmatched middle docs collapse into "+N more".
        self.assertEqual(overflow, 2)

    def test_relevant_row_sorts_first(self):
        rows, _ = select_inventory(self._index(), "stainless steel fastener", recent_cap=2)
        self.assertTrue(rows[0].possibly_related)


class DocumentIdentityTests(unittest.TestCase):
    """#8 / #4 — a held document's ref IS the sources-registry id, not a parallel id space (RP-9)."""

    def test_ref_matches_the_sources_registry(self):
        index = DocumentIndex.from_manifest(CORPUS / "documents.jsonl")
        registry = SourcesRegistry(CORPUS / "sources.jsonl")
        for doc in index.documents():
            self.assertIsNotNone(
                registry.get(doc.ref),
                f"document {doc.ref} should resolve to a registered source",
            )


if __name__ == "__main__":
    unittest.main()
