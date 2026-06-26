"""Bidirectional cross-topic links (design decision #9).

The obvious answer to "how do facts about related things stay connected" is a graph database. That's the
wrong size here: the entities already exist — topic routing (decisions #1–#7) already resolved *which
file* a fact belongs in. What's missing isn't an index, it's a cheap, structural way to record "topic A
also relates to topic B" using the same write path everything else uses.

A link is **two independently-recorded markdown lines**, not one — a forward marker in A's section and a
"referenced by" marker in B's section. That's deliberate, double-entry framing: a link that's only
*derived* from one side (recomputed at read time) is consistent by construction and can never drift, so
there's nothing to check. Two independently-written sides *can* drift — and a mismatch between them is
exactly the signal :func:`reconcile` is built to catch.

The check itself doesn't need an LLM. Detecting *that* two topics relate is a judgment call (production
piggybacks it on the extraction routing decision; this repo's demo bakes the decision in as a labeled
fixture — see ``examples/demo.py``). Reconciling an existing link — does the forward marker have a
matching reverse marker, has the linked-to section's substantive content drifted since the link was
made — is mechanical: a regex match and a hash comparison, the same shape as :mod:`memory.consolidator`'s
dedup + re-tier passes.

Publication boundary
---------------------
Everything here is structural, not domain-tuned calibration — unlike decision #8's overlap threshold,
there's nothing to withhold. The marker template, the hash-stripping fix (see :func:`_strip_markers`),
and the reconciliation logic are exactly what shipped in production.

Described-only (not in this runnable core): rewriting markers when a topic is renamed, merged, or split.
This repo's consolidator already treats topic-reorg as described-only (see ``consolidator.py``'s module
docstring); link rewriting on reorg inherits that same boundary. See ``docs/architecture.md`` for the
production design (rename/split/merge/dissolve all reuse the same old→new section mapping the reorg pass
already computes for its own bookkeeping).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from memory.store import MemoryStore

_FORWARD_TEMPLATE = "- See also: [[topic:{topic}]] § {heading}"
_REVERSE_TEMPLATE = "- Referenced by: [[topic:{topic}]] § {heading}"
# Matches either marker line, capturing the topic + heading it points at.
_MARKER_RE = re.compile(r"^- (?:See also|Referenced by): \[\[topic:([^\]]+)\]\] § (.+)$", re.MULTILINE)

_H2_RE = re.compile(r"^##\s+(?P<heading>.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Link:
    """One bidirectional link, keyed by both endpoints.

    ``content_hash`` snapshots topic_b's section *with marker lines stripped out* (see
    :func:`_strip_markers`) at link-creation time — hashing the raw section would make every link
    "stale" the instant it's created, since adding the reverse marker is itself a write to that section.
    """

    topic_a: str
    heading_a: str
    topic_b: str
    heading_b: str
    content_hash: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.topic_a, self.heading_a, self.topic_b, self.heading_b)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass(frozen=True)
class LinkIssue:
    """A single reconciliation finding: a marker missing on one side, or the linked-to content drifted."""

    link: Link
    kind: str  # "missing_forward" | "missing_reverse" | "stale"


class LinksRegistry:
    """File-backed ``backlinks.jsonl`` registry — same shape as :class:`evidence.sources.SourcesRegistry`.

    Append is idempotent by key (topic_a, heading_a, topic_b, heading_b); registering an existing link
    re-snapshots its content_hash (the link was just re-confirmed at the current content).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._by_key: dict[tuple[str, str, str, str], Link] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    link = Link(**json.loads(line))
                    self._by_key[link.key()] = link

    def all(self) -> list[Link]:
        return list(self._by_key.values())

    def upsert(self, link: Link) -> None:
        self._by_key[link.key()] = link
        self._save()

    def _save(self) -> None:
        self.path.write_text(
            "\n".join(link.to_json() for link in self._by_key.values()) + "\n",
            encoding="utf-8",
        )


def _strip_markers(content: str) -> str:
    """Remove backlink marker lines before hashing, so creating/removing a link is never itself
    counted as substantive content drift."""
    lines = [line for line in content.split("\n") if not _MARKER_RE.match(line)]
    return "\n".join(lines)


def _section_content(markdown: str, heading: str) -> str | None:
    sections = {m.group("heading"): m for m in _H2_RE.finditer(markdown)}
    if heading not in sections:
        return None
    starts = sorted(m.start() for m in sections.values())
    start = sections[heading].end()
    later = [s for s in starts if s > sections[heading].start()]
    end = later[0] if later else len(markdown)
    return markdown[start:end].strip("\n")


def _append_to_section(markdown: str, heading: str, line: str) -> str:
    """Append one line to the end of an H2 section's content. Section must already exist."""
    pattern = re.compile(rf"(^##\s+{re.escape(heading)}\s*$\n)((?:.*\n?)*?)(?=^## |\Z)", re.MULTILINE)
    m = pattern.search(markdown)
    if not m:
        raise ValueError(f"section {heading!r} not found")
    body = m.group(2).rstrip("\n")
    new_body = f"{body}\n{line}\n" if body else f"{line}\n"
    return markdown[: m.start(2)] + new_body + markdown[m.end(2):]


def render_link(store: MemoryStore, topic_a: str, heading_a: str, topic_b: str, heading_b: str) -> Link:
    """Write a bidirectional link: a forward marker in A, a "referenced by" marker in B.

    Idempotent — re-rendering an existing link is a no-op on the markdown (the marker line already
    matches) but re-snapshots the content_hash, since calling this again means the link was just
    reconfirmed against B's current content.
    """
    text_b = store.read_topic(topic_b)
    section_b = _section_content(text_b, heading_b)
    if section_b is None:
        raise ValueError(f"{topic_b!r} has no section {heading_b!r}")
    content_hash = hashlib.sha256(_strip_markers(section_b).encode("utf-8")).hexdigest()

    forward = _FORWARD_TEMPLATE.format(topic=topic_b, heading=heading_b)
    reverse = _REVERSE_TEMPLATE.format(topic=topic_a, heading=heading_a)

    text_a = store.read_topic(topic_a)
    if forward not in text_a:
        store.write_topic(topic_a, _append_to_section(text_a, heading_a, forward))
    if reverse not in text_b:
        store.write_topic(topic_b, _append_to_section(text_b, heading_b, reverse))

    return Link(topic_a, heading_a, topic_b, heading_b, content_hash)


def reconcile(store: MemoryStore, links: list[Link]) -> list[LinkIssue]:
    """Deterministic double-entry check over every recorded link — no LLM judgment.

    For each link: does A still carry the forward marker, does B still carry the reverse marker, and has
    B's section drifted (by substantive content, markers excluded) since the link was made.
    """
    issues: list[LinkIssue] = []
    for link in links:
        text_a = store.read_topic(link.topic_a) if store.has_topic(link.topic_a) else ""
        section_a = _section_content(text_a, link.heading_a) or ""
        has_forward = any(
            m.group(1) == link.topic_b and m.group(2) == link.heading_b
            for m in _MARKER_RE.finditer(section_a)
        )
        if not has_forward:
            issues.append(LinkIssue(link, "missing_forward"))

        if not store.has_topic(link.topic_b):
            issues.append(LinkIssue(link, "missing_reverse"))
            continue
        text_b = store.read_topic(link.topic_b)
        section_b = _section_content(text_b, link.heading_b)
        if section_b is None:
            issues.append(LinkIssue(link, "missing_reverse"))
            continue
        has_reverse = any(
            m.group(1) == link.topic_a and m.group(2) == link.heading_a
            for m in _MARKER_RE.finditer(section_b)
        )
        if not has_reverse:
            issues.append(LinkIssue(link, "missing_reverse"))
            continue

        current_hash = hashlib.sha256(_strip_markers(section_b).encode("utf-8")).hexdigest()
        if current_hash != link.content_hash:
            issues.append(LinkIssue(link, "stale"))

    return issues
