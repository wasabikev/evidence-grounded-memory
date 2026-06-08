"""Behavior tests for the evidence layer: tier vocabulary (#3) and sources registry / tags (#4)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence.sources import (  # noqa: E402
    Source,
    SourcesRegistry,
    build_tag,
    iter_tags,
    parse_tag,
    source_from_category,
)
from evidence.tiers import is_provisional, is_stronger, resolve_tier, tier_rank  # noqa: E402


class TierVocabularyTests(unittest.TestCase):
    def test_category_resolves_to_tier(self):
        self.assertEqual(resolve_tier("regulation"), "A")
        self.assertEqual(resolve_tier("official_guidance"), "B")
        self.assertEqual(resolve_tier("model_inference"), "G")

    def test_unknown_category_is_rejected(self):
        # The published skeleton is closed; classifying raw text into a category is the withheld step.
        with self.assertRaises(ValueError):
            resolve_tier("vibes")

    def test_tier_ordering(self):
        self.assertTrue(is_stronger("A", "G"))
        self.assertTrue(is_stronger("B", "F"))
        self.assertFalse(is_stronger("E", "C"))
        self.assertLess(tier_rank("A"), tier_rank("D"))

    def test_user_and_ai_are_provisional(self):
        self.assertTrue(is_provisional("F"))
        self.assertTrue(is_provisional("G"))
        self.assertFalse(is_provisional("A"))


class TagGrammarTests(unittest.TestCase):
    def test_doc_and_web_roundtrip(self):
        for kind, ref, tier in [("doc", "e037", "A"), ("web", "e072", "E")]:
            tag = build_tag(kind, ref, tier)
            self.assertEqual(parse_tag(tag), (kind, ref, tier))

    def test_user_and_ai_are_tier_fixed_by_kind(self):
        self.assertEqual(parse_tag("[user]"), ("user", None, "F"))
        self.assertEqual(parse_tag("[ai]"), ("ai", None, "G"))
        self.assertEqual(build_tag("user"), "[user]")
        self.assertEqual(build_tag("ai"), "[ai]")

    def test_middle_dot_separator(self):
        # The ref/tier separator is the middle dot (·, U+00B7), not an ASCII period or hyphen.
        self.assertEqual(build_tag("doc", "e037", "A"), "[doc:e037·A]")

    def test_malformed_tag_rejected(self):
        with self.assertRaises(ValueError):
            parse_tag("[doc:e037-A]")  # wrong separator

    def test_iter_tags_over_a_bullet(self):
        line = "Stainless fasteners are recommended near salt air. [doc:e055·C]"
        self.assertEqual(iter_tags(line), [("doc", "e055", "C")])


class SourcesRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="egm_reg_"))
        self.path = self.tmp / "sources.jsonl"

    def test_register_is_idempotent_by_ref(self):
        reg = SourcesRegistry(self.path)
        s = source_from_category("e037", "Building code", "regulation")
        reg.register(s)
        # A second register of the same ref must not append a duplicate row.
        reg.register(source_from_category("e037", "DIFFERENT label", "official_guidance"))
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(reg.lookup("e037").label, "Building code")

    def test_tier_derived_from_category(self):
        s = source_from_category("e060", "Contractor quote", "contract")
        self.assertEqual(s.tier, "D")

    def test_update_regrades_and_persists(self):
        reg = SourcesRegistry(self.path)
        reg.register(source_from_category("e072", "Reviews", "aggregated_reviews"))
        reg.update("e072", tier="D")
        # Reload from disk to confirm the re-grade was written through, not just held in memory.
        self.assertEqual(SourcesRegistry(self.path).lookup("e072").tier, "D")

    def test_lookup_unknown_raises(self):
        with self.assertRaises(KeyError):
            SourcesRegistry(self.path).lookup("nope")

    def test_get_returns_none_for_unknown(self):
        self.assertIsNone(SourcesRegistry(self.path).get("nope"))


if __name__ == "__main__":
    unittest.main()
