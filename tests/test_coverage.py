"""Behavior tests for the proactive coverage sweep (#10). Each test is an executable claim about the
decision.

  - Primary structurally never reaches a stale conversation — it fires on new messages, and a stale
    conversation isn't receiving any
  - Secondary reaches at most one conversation out of an entire stale backlog: whichever was created most
    recently, leaving older stale conversations permanently uncovered
  - the sweep enumerates the full stale-and-unsummarized set directly, capped and deduplicated against
    work already in flight
  - the residual (what the sweep catches that neither reactive trigger does) is the whole point — this is
    the coverage gap made visible

No fixture corpus, no persisted conversation concept (OQ-2) — a pure function over an inline candidate
list, the same as ``examples/demo.py``'s own act 7.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.coverage import (  # noqa: E402
    ConversationCandidate,
    coverage_report,
    primary_would_catch,
    secondary_would_catch,
    stale_unsummarized,
    sweep_would_catch,
)

NOW = datetime.fromisoformat("2026-08-13T00:00:00")


def candidate(cid: str, *, created: str, last_message: str, has_summary: bool = False) -> ConversationCandidate:
    return ConversationCandidate(cid, created_at=created, last_message_at=last_message, has_summary=has_summary)


class StaleUnsummarizedTests(unittest.TestCase):
    """The candidate set any trigger is trying to reach: unsummarized AND idle past the threshold."""

    def test_summarized_conversation_is_excluded_even_if_idle(self):
        stale = stale_unsummarized(
            [candidate("c1", created="2026-06-01", last_message="2026-06-01", has_summary=True)],
            now=NOW,
            idle_threshold_hours=24,
        )
        self.assertEqual(stale, [])

    def test_recent_unsummarized_conversation_is_excluded(self):
        stale = stale_unsummarized(
            [candidate("c1", created="2026-08-12T23:00:00", last_message="2026-08-12T23:30:00")],
            now=NOW,
            idle_threshold_hours=24,
        )
        self.assertEqual(stale, [])

    def test_old_unsummarized_conversation_is_included(self):
        stale = stale_unsummarized(
            [candidate("c1", created="2026-05-01", last_message="2026-05-01")],
            now=NOW,
            idle_threshold_hours=24,
        )
        self.assertEqual([c.conversation_id for c in stale], ["c1"])


class PrimaryTests(unittest.TestCase):
    """#10 — Primary is structurally reactive to the *same* conversation; it never reaches a stale one."""

    def test_never_catches_anything_in_a_stale_set(self):
        stale = [
            candidate("c1", created="2026-05-01", last_message="2026-05-01"),
            candidate("c2", created="2026-05-02", last_message="2026-05-02"),
        ]
        self.assertEqual(primary_would_catch(stale), frozenset())


class SecondaryTests(unittest.TestCase):
    """#10 — Secondary reaches at most one conversation: the most recently created."""

    def test_catches_only_the_most_recently_created(self):
        stale = [
            candidate("older", created="2026-05-01", last_message="2026-05-01"),
            candidate("newest", created="2026-06-01", last_message="2026-05-15"),
            candidate("middle", created="2026-05-15", last_message="2026-05-10"),
        ]
        self.assertEqual(secondary_would_catch(stale), frozenset({"newest"}))

    def test_empty_backlog_catches_nothing(self):
        self.assertEqual(secondary_would_catch([]), frozenset())


class SweepTests(unittest.TestCase):
    """#10 — the sweep enumerates the full set directly, capped and deduplicated."""

    def test_catches_the_whole_backlog_when_uncapped(self):
        stale = [
            candidate("c1", created="2026-05-01", last_message="2026-05-01"),
            candidate("c2", created="2026-05-02", last_message="2026-05-02"),
            candidate("c3", created="2026-05-03", last_message="2026-05-03"),
        ]
        self.assertEqual(sweep_would_catch(stale), frozenset({"c1", "c2", "c3"}))

    def test_cap_takes_the_oldest_idle_first(self):
        stale = [
            candidate("newer_idle", created="2026-05-01", last_message="2026-05-10"),
            candidate("oldest_idle", created="2026-05-01", last_message="2026-05-01"),
        ]
        # Oldest-idle-first ordering means a repeated capped pass eventually clears the whole backlog
        # instead of the same conversations winning the cap every time.
        self.assertEqual(sweep_would_catch(stale, cap=1), frozenset({"oldest_idle"}))

    def test_already_in_flight_is_deduplicated_out(self):
        stale = [candidate("c1", created="2026-05-01", last_message="2026-05-01")]
        self.assertEqual(
            sweep_would_catch(stale, already_in_flight=frozenset({"c1"})),
            frozenset(),
        )


class CoverageReportTests(unittest.TestCase):
    """#10 — the full comparison, and the residual that's the actual point of the decision."""

    def test_residual_is_what_only_the_sweep_reaches(self):
        candidates = [
            # Rescued by Secondary (most recently created of the stale set).
            candidate("secondary_catch", created="2026-06-01", last_message="2026-05-01"),
            # Falls through both reactive triggers -- only the sweep reaches it.
            candidate("gap", created="2026-01-01", last_message="2026-01-01"),
            # Not stale -- recently active, excluded from every set.
            candidate("active", created="2026-08-12", last_message="2026-08-12T23:00:00"),
        ]
        report = coverage_report(candidates, now=NOW, idle_threshold_hours=24)
        self.assertEqual(report.stale_ids, frozenset({"secondary_catch", "gap"}))
        self.assertEqual(report.primary_ids, frozenset())
        self.assertEqual(report.secondary_ids, frozenset({"secondary_catch"}))
        self.assertEqual(report.sweep_ids, frozenset({"secondary_catch", "gap"}))
        self.assertEqual(report.residual_ids, frozenset({"gap"}))

    def test_no_gap_means_empty_residual(self):
        # A single stale conversation: Secondary alone already covers it, so the sweep's residual is empty
        # -- the gap only shows up with more than one stale conversation competing for Secondary's one slot.
        candidates = [candidate("only_one", created="2026-01-01", last_message="2026-01-01")]
        report = coverage_report(candidates, now=NOW, idle_threshold_hours=24)
        self.assertEqual(report.residual_ids, frozenset())


if __name__ == "__main__":
    unittest.main()
