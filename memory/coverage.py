"""Reactive-vs-proactive trigger coverage comparison (design decision #10).

Two reactive triggers look like coverage until you ask *whose activity* each one depends on. Primary fires
when a new message lands in a conversation — structurally, that means it can never reach a conversation
that's gone quiet; a stale, abandoned conversation is by definition one nothing is happening in. Secondary
fires when a *different* new conversation starts, and re-checks only the single most-recently-created other
conversation — never the backlog. A user who never starts another conversation falls through both. A user
with several stale conversations only ever gets the newest one re-checked by Secondary; the rest sit
uncovered indefinitely.

The fix isn't a third variation on "wait for the user to do something" — it's a structurally independent
sweep that enumerates the actual candidate set (every conversation past its idle threshold with no summary
yet) directly, instead of waiting to be triggered by it. :func:`coverage_report` is the comparison that
makes the gap legible: given a candidate set, show what each reactive trigger would catch, then show what
the sweep catches that neither would have reached. The residual is the point — a repository that can name
its own blind spots is doing something a purely reactive design structurally cannot.

Publication boundary
---------------------
The trigger-comparison logic here — what each reactive trigger structurally can and can't reach, and how
the sweep enumerates + caps + dedupes its own candidate set — is exactly what production does; nothing
about it is domain-tuned calibration. The scheduling *cadence* (hourly), *cap* (20 conversations/pass), and
*retry limit* (3 attempts before a conversation drops out of re-candidacy) are published here as real
production values, not illustrative ones — see ``docs/architecture.md`` §10. None of the three are
calibrated against real client data, so there's nothing to withhold; the "candor is a feature" treatment
this repo gives its other low-sensitivity decisions applies here too.

Described-only (not in this runnable core): the actual scheduling substrate — a background task queue, a
Redis-persisted resume timestamp across restarts, and the attempt-count bookkeeping that retires a
conversation from re-candidacy after repeated failures. This repo has no scheduler/task-queue concept at
all (the same standing boundary ``memory/consolidator.py`` already draws around production's task
orchestration) — what's runnable here is the comparison logic those production mechanics sit on top of.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ConversationCandidate:
    """One conversation as a coverage trigger would see it — the observable state it reasons over.

    ``created_at`` determines Secondary's "most recently created other conversation" pick;
    ``last_message_at`` is what staleness is measured from. Both are ISO 8601 strings, sortable as-is.
    """

    conversation_id: str
    created_at: str
    last_message_at: str
    has_summary: bool


@dataclass(frozen=True)
class CoverageReport:
    """The comparison: what each trigger would catch out of the same stale, unsummarized candidate set."""

    stale_ids: frozenset[str]
    primary_ids: frozenset[str]
    secondary_ids: frozenset[str]
    sweep_ids: frozenset[str]

    @property
    def residual_ids(self) -> frozenset[str]:
        """Conversations only the proactive sweep reaches — what the two reactive triggers structurally
        cannot, no matter how long the corpus sits untouched."""
        return self.sweep_ids - self.primary_ids - self.secondary_ids


def stale_unsummarized(
    candidates: list[ConversationCandidate],
    *,
    now: datetime,
    idle_threshold_hours: float,
) -> list[ConversationCandidate]:
    """The actual candidate set any trigger is trying to reach: unsummarized, and idle past the threshold.

    ``now`` is always caller-supplied, never wall-clock — the comparison has to be reproducible, not
    dependent on when the demo happens to run.
    """
    cutoff = now - timedelta(hours=idle_threshold_hours)
    return [
        c
        for c in candidates
        if not c.has_summary and datetime.fromisoformat(c.last_message_at) <= cutoff
    ]


def primary_would_catch(stale: list[ConversationCandidate]) -> frozenset[str]:
    """Primary fires on a new message in *the same* conversation. A conversation in the stale set is, by
    definition, one that isn't receiving new messages — so Primary structurally never reaches it. This
    isn't a simplification of the mechanism; it's the mechanism. Primary's real job is keeping an
    *actively used* conversation summarized, not rescuing an abandoned one — which is exactly the gap
    decision #10 exists to close."""
    return frozenset()


def secondary_would_catch(stale: list[ConversationCandidate]) -> frozenset[str]:
    """Secondary fires when a different new conversation starts, and re-checks only the single
    most-recently-created *other* conversation. Out of an entire stale backlog, it can reach at most one —
    whichever was created most recently — leaving every older stale conversation permanently uncovered."""
    if not stale:
        return frozenset()
    newest = max(stale, key=lambda c: c.created_at)
    return frozenset({newest.conversation_id})


def sweep_would_catch(
    stale: list[ConversationCandidate],
    *,
    cap: int | None = None,
    already_in_flight: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """The proactive sweep enumerates the candidate set directly — capped per pass, deduplicated against
    work already scheduled — rather than waiting for a reactive trigger to happen to touch it. Ordered
    oldest-idle-first so, across repeated passes, the whole backlog eventually clears rather than the same
    conversations winning the cap every time."""
    ordered = sorted(
        (c for c in stale if c.conversation_id not in already_in_flight),
        key=lambda c: c.last_message_at,
    )
    if cap is not None:
        ordered = ordered[:cap]
    return frozenset(c.conversation_id for c in ordered)


def coverage_report(
    candidates: list[ConversationCandidate],
    *,
    now: datetime,
    idle_threshold_hours: float,
    cap: int | None = None,
    already_in_flight: frozenset[str] = frozenset(),
) -> CoverageReport:
    """Run the full comparison: the stale candidate set, what each trigger catches, and (via
    :attr:`CoverageReport.residual_ids`) what only the sweep reaches."""
    stale = stale_unsummarized(candidates, now=now, idle_threshold_hours=idle_threshold_hours)
    stale_ids = frozenset(c.conversation_id for c in stale)
    return CoverageReport(
        stale_ids=stale_ids,
        primary_ids=primary_would_catch(stale),
        secondary_ids=secondary_would_catch(stale),
        sweep_ids=sweep_would_catch(stale, cap=cap, already_in_flight=already_in_flight),
    )
