"""Behavior tests for document recall (#8). Each test is an executable claim about the decision.

  - content recall: a query whose answer lives in a document *body* finds it, where filename-only
    matching (the production bug) misses — the core of #8
  - identity: a document's ref is the same id the sources registry (#4) uses — not a separate id space

Runs against the committed demo corpus, so a fixture change that breaks the claim breaks the test.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from documents.index import DocumentIndex  # noqa: E402
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
