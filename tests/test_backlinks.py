"""Behavior tests for cross-topic backlinks (#9). Each test is an executable claim about the decision.

  - rendering: a link writes an independently-recorded marker on *both* sides, not a derived/recomputed
    one — and re-rendering an existing link is idempotent (no duplicate marker lines)
  - reconciliation is deterministic (no LLM): it correctly reports a freshly-created link as clean,
    flags a marker that's gone missing on either side, and flags genuine content drift on the linked-to
    section — while *not* false-flagging the act of creating the link itself as drift (the bug this
    repo's production counterpart actually shipped with, caught by testing against real data)
  - the registry round-trips through ``backlinks.jsonl``, same shape as the sources registry (#4)

Uses an isolated in-memory-style store (``make_store``), not the committed demo corpus — the mechanism
under test doesn't depend on the home-renovation domain.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.backlinks import Link, LinksRegistry, reconcile, render_link  # noqa: E402
from memory.store import MemoryStore  # noqa: E402

TOPIC_A = "## Topic A\n- a fact about A. [user]\n"
TOPIC_B = "## Topic B\n- a fact about B. [user]\n"


def make_store() -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="egm_test_backlinks_"))
    store = MemoryStore(tmp)
    store.write_topic("a", TOPIC_A)
    store.write_topic("b", TOPIC_B)
    store.rebuild_index()
    return store


class RenderLinkTests(unittest.TestCase):
    """#9 — a link is two independently-recorded markers, not one derived view."""

    def test_writes_a_marker_on_both_sides(self):
        store = make_store()
        render_link(store, "a", "Topic A", "b", "Topic B")
        self.assertIn("See also: [[topic:b]] § Topic B", store.get_section("a", "Topic A").body)
        self.assertIn("Referenced by: [[topic:a]] § Topic A", store.get_section("b", "Topic B").body)

    def test_re_rendering_is_idempotent(self):
        store = make_store()
        render_link(store, "a", "Topic A", "b", "Topic B")
        render_link(store, "a", "Topic A", "b", "Topic B")
        forward_count = store.get_section("a", "Topic A").body.count("See also: [[topic:b]] § Topic B")
        self.assertEqual(forward_count, 1)


class ReconcileTests(unittest.TestCase):
    """#9 — deterministic double-entry check: no LLM, just a regex match and a hash comparison."""

    def test_fresh_link_is_clean(self):
        store = make_store()
        link = render_link(store, "a", "Topic A", "b", "Topic B")
        # Regression guard: the snapshot must exclude the marker lines being written, or every fresh
        # link would read "stale" the instant it's created (the act of linking is itself a write).
        self.assertEqual(reconcile(store, [link]), [])

    def test_missing_forward_marker_is_flagged(self):
        store = make_store()
        link = render_link(store, "a", "Topic A", "b", "Topic B")
        # Simulate the marker being lost (e.g. a manual edit) by rewriting A without it.
        store.write_topic("a", TOPIC_A)
        issues = reconcile(store, [link])
        self.assertEqual([i.kind for i in issues], ["missing_forward"])

    def test_missing_reverse_marker_is_flagged(self):
        store = make_store()
        link = render_link(store, "a", "Topic A", "b", "Topic B")
        store.write_topic("b", TOPIC_B)
        issues = reconcile(store, [link])
        self.assertEqual([i.kind for i in issues], ["missing_reverse"])

    def test_genuine_drift_on_linked_section_is_flagged_stale(self):
        store = make_store()
        link = render_link(store, "a", "Topic A", "b", "Topic B")
        text_b = store.read_topic("b")
        store.write_topic("b", text_b.replace("a fact about B.", "a DIFFERENT fact about B."))
        issues = reconcile(store, [link])
        self.assertEqual([i.kind for i in issues], ["stale"])

    def test_target_topic_deleted_is_flagged_missing_reverse(self):
        store = make_store()
        link = render_link(store, "a", "Topic A", "b", "Topic B")
        store.write_topic("b", "## A Different Section\n- nothing here.\n")
        issues = reconcile(store, [link])
        self.assertEqual([i.kind for i in issues], ["missing_reverse"])


class LinksRegistryTests(unittest.TestCase):
    """Same file-backed shape as evidence.sources.SourcesRegistry — jsonl, idempotent by key."""

    def test_round_trips_through_disk(self):
        tmp = Path(tempfile.mkdtemp(prefix="egm_test_backlinks_"))
        path = tmp / "backlinks.jsonl"
        registry = LinksRegistry(path)
        link = Link("a", "Topic A", "b", "Topic B", content_hash="abc123")
        registry.upsert(link)

        reloaded = LinksRegistry(path)
        self.assertEqual(reloaded.all(), [link])

    def test_upsert_replaces_by_key(self):
        tmp = Path(tempfile.mkdtemp(prefix="egm_test_backlinks_"))
        registry = LinksRegistry(tmp / "backlinks.jsonl")
        registry.upsert(Link("a", "Topic A", "b", "Topic B", content_hash="old"))
        registry.upsert(Link("a", "Topic A", "b", "Topic B", content_hash="new"))
        self.assertEqual(len(registry.all()), 1)
        self.assertEqual(registry.all()[0].content_hash, "new")


if __name__ == "__main__":
    unittest.main()
