"""File-first markdown store + section-level FTS5 index (decisions #1 keyword path, #7; #5 primitives).

Markdown topic files (``<root>/topics/*.md``, H2-sectioned) are the **source of truth**. This module
owns topic CRUD, a derived SQLite **FTS5 index built at the section level** (not the document level), and
the non-destructive **annotation primitives** that temporal re-grading (#5) is built on.

The index is disposable by construction: :meth:`MemoryStore.rebuild_index` drops and rebuilds it from the
markdown at any time, with no data loss. The markdown is the only authority.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_H2_RE = re.compile(r"^##\s+(?P<heading>.+?)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+")
# FTS5 query tokens: keep alphanumerics and the dots inside codes like "R105.2".
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.]*")


@dataclass(frozen=True)
class Section:
    """One H2 section of a topic file — the unit of indexing and injection."""

    topic: str
    heading: str
    body: str

    @property
    def id(self) -> str:
        return f"{self.topic}#{self.heading}"

    @property
    def bullets(self) -> list[str]:
        return [ln.strip() for ln in self.body.splitlines() if _BULLET_RE.match(ln)]


@dataclass(frozen=True)
class SectionMatch:
    """A ranked section result. ``relevance`` is higher-is-better (FTS5 bm25 is negated)."""

    section: Section
    relevance: float


def split_sections(topic: str, markdown: str) -> list[Section]:
    """Split a topic's markdown into its H2 sections (text before the first H2 is ignored — title only)."""
    sections: list[Section] = []
    matches = list(_H2_RE.finditer(markdown))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip("\n")
        sections.append(Section(topic=topic, heading=m.group("heading"), body=body))
    return sections


def build_fts_query(query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression.

    Each whitespace token is phrase-quoted (so ``R105.2`` becomes the adjacent phrase ``"r105 2"`` under
    the default tokenizer, preserving exact-code matching) and the tokens are OR-ed together.
    """
    phrases = []
    for raw in query.split():
        toks = _WORD_RE.findall(raw)
        if toks:
            phrases.append('"' + " ".join(toks).lower() + '"')
    return " OR ".join(phrases)


class MemoryStore:
    """File-first topic store with a derived section-level keyword (FTS5) index."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.topics_dir = self.root / "topics"
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "section_index.db"
        self._conn: sqlite3.Connection | None = None

    # --- topic CRUD (the source of truth) --------------------------------------------------------
    def _topic_path(self, name: str) -> Path:
        return self.topics_dir / f"{name}.md"

    def write_topic(self, name: str, markdown: str) -> None:
        self._topic_path(name).write_text(markdown.rstrip("\n") + "\n", encoding="utf-8")

    def read_topic(self, name: str) -> str:
        return self._topic_path(name).read_text(encoding="utf-8")

    def has_topic(self, name: str) -> bool:
        return self._topic_path(name).exists()

    def list_topics(self) -> list[str]:
        return sorted(p.stem for p in self.topics_dir.glob("*.md"))

    def iter_sections(self) -> list[Section]:
        out: list[Section] = []
        for name in self.list_topics():
            out.extend(split_sections(name, self.read_topic(name)))
        return out

    def get_section(self, topic: str, heading: str) -> Section | None:
        for sec in split_sections(topic, self.read_topic(topic)):
            if sec.heading == heading:
                return sec
        return None

    # --- derived FTS5 section index (disposable) --------------------------------------------------
    def rebuild_index(self) -> None:
        """Drop and rebuild the section-level FTS5 index from the markdown topic files."""
        if self._conn is not None:
            self._conn.close()
        # A fresh DB every rebuild underlines that the index is derived/disposable.
        if self.db_path.exists():
            self.db_path.unlink()
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            "CREATE VIRTUAL TABLE sections USING fts5(topic, heading, body, tokenize='unicode61')"
        )
        rows = [(s.topic, s.heading, s.body) for s in self.iter_sections()]
        self._conn.executemany("INSERT INTO sections(topic, heading, body) VALUES (?, ?, ?)", rows)
        self._conn.commit()

    def _require_index(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("call rebuild_index() before searching")
        return self._conn

    def search_sections(self, query: str, limit: int = 8) -> list[SectionMatch]:
        """BM25-ranked section matches for a keyword query — the keyword half of dual-path recall (#1).

        Returns higher-is-better ``relevance`` (SQLite ``bm25`` is smaller-is-better, so it is negated).
        """
        match = build_fts_query(query)
        if not match:
            return []
        conn = self._require_index()
        cur = conn.execute(
            "SELECT topic, heading, body, bm25(sections) AS rank "
            "FROM sections WHERE sections MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        )
        return [
            SectionMatch(Section(topic=t, heading=h, body=b), relevance=-rank)
            for (t, h, b, rank) in cur.fetchall()
        ]

    # --- temporal re-grading primitives (#5) — non-destructive edits to the source of truth -------
    def confirm_bullet(self, topic: str, heading: str, needle: str, ref: str) -> bool:
        """Promote a provisional bullet: append ``(confirmed by <ref>)`` to the bullet containing ``needle``.

        Idempotent — a bullet already confirmed by ``ref`` is left unchanged. Returns True if a bullet was
        matched. Caller is responsible for re-running :meth:`rebuild_index` afterward.
        """
        annotation = f"(confirmed by {ref})"
        return self._annotate_bullet(topic, heading, needle, annotation, skip_if_present=annotation)

    def supersede_bullet(
        self,
        topic: str,
        heading: str,
        old_needle: str,
        new_bullet: str,
        ref: str,
        date: str,
    ) -> bool:
        """Record a supersession *bidirectionally and non-destructively*.

        The old bullet (matched by ``old_needle``) gains ``(superseded <date> by <ref>)`` — it is **never
        deleted**, preserving the audit trail of what was believed. A new bullet is appended to the same
        section carrying ``(supersedes: <old text>)``. Returns True if the old bullet was found.
        """
        old_line = self._find_bullet_line(topic, heading, old_needle)
        if old_line is None:
            return False
        old_clean = _strip_bullet_marker(old_line)
        marked_old = f"{old_line.rstrip()} (superseded {date} by {ref})"
        new_line = f"- {new_bullet.strip()} (supersedes: {_summarize(old_clean)})"
        markdown = self.read_topic(topic)
        markdown = self._replace_in_section(markdown, heading, old_line, marked_old + "\n" + new_line)
        self.write_topic(topic, markdown)
        return True

    # --- internal markdown surgery ---------------------------------------------------------------
    def _annotate_bullet(
        self, topic: str, heading: str, needle: str, annotation: str, *, skip_if_present: str
    ) -> bool:
        line = self._find_bullet_line(topic, heading, needle)
        if line is None:
            return False
        if skip_if_present in line:
            return True  # idempotent: already annotated
        markdown = self.read_topic(topic)
        markdown = self._replace_in_section(markdown, heading, line, f"{line.rstrip()} {annotation}")
        self.write_topic(topic, markdown)
        return True

    def _find_bullet_line(self, topic: str, heading: str, needle: str) -> str | None:
        section = self.get_section(topic, heading)
        if section is None:
            return None
        for raw in section.body.splitlines():
            if _BULLET_RE.match(raw) and needle.lower() in raw.lower():
                return raw
        return None

    def _replace_in_section(self, markdown: str, heading: str, old_line: str, new_block: str) -> str:
        # Replace within the target section only, to avoid touching an identical line elsewhere.
        sections = list(_H2_RE.finditer(markdown))
        for i, m in enumerate(sections):
            if m.group("heading") == heading:
                start = m.end()
                end = sections[i + 1].start() if i + 1 < len(sections) else len(markdown)
                body = markdown[start:end]
                body = body.replace(old_line, new_block, 1)
                return markdown[:start] + body + markdown[end:]
        raise KeyError(f"section {heading!r} not found in topic")


def _strip_bullet_marker(line: str) -> str:
    return _BULLET_RE.sub("", line).strip()


def _summarize(text: str, limit: int = 60) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
