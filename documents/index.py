"""Per-agent derived document index — the searchable half of document recall (decision #8).

Source documents (uploaded files) are a second knowledge substrate alongside topic memory: their
*extracted text* is the source of truth, and a derived SQLite **FTS5 index** over filename + gist + body
makes their content recallable. Like the topic index in ``memory/store.py``, it is disposable by
construction — rebuilt from the documents manifest + body files at any time.

Why this is decision #8 and not "search, again": the novelty isn't FTS5 — that's the *same standard
retrieval* as the keyword path in #1, deliberately reused here. The decision is that the **content of held
documents becomes addressable at all**, so the knowledge repository can surface its own primary sources
instead of relying on the agent to remember a search tool. The production failure this fixes was
filename-only matching: a document whose answer lives in its body went unfound because its *name* didn't
contain the query terms.

Scope of this reference: production keys one index **per agent** so documents never leak across agents;
that structural isolation is described in the architecture but not exercised by this single-tenant demo,
which demonstrates the discovery + content-recall half.

Fixtures note: in production a document's text comes from OCR (``processed_text``) and its gist from a
utility model (``ai_summary``). This offline demo commits both directly — real design, illustrative data.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Reuse the exact safe-MATCH builder the keyword path uses (#1): same retrieval, new substrate.
from memory.store import build_fts_query


@dataclass(frozen=True)
class Document:
    """One held document: a registry ref with extracted text, mirroring production's metadata/body split.

    ``filename`` + ``gist`` mirror an ``UploadedFile`` row's filename / ``ai_summary``; ``body`` mirrors its
    extracted ``processed_text``. ``ref`` is the **same id used in the sources registry** (#4), so a
    ``[doc:e060·D]`` citation resolves to recallable content rather than a separate document-id space.
    """

    ref: str
    filename: str
    gist: str
    body: str
    date: str | None = None


@dataclass(frozen=True)
class DocumentMatch:
    """A ranked document result. ``relevance`` is higher-is-better (bm25 negated), as in ``SectionMatch``."""

    document: Document
    relevance: float


def load_documents(manifest_path: str | Path) -> list[Document]:
    """Load the manifest (``documents.jsonl``) and each ref's body (``documents/<ref>.txt``).

    Mirrors production's split: structured metadata in the manifest row, extracted text in a sibling file.
    A manifest row whose body file is missing is skipped — a body-less registry entry (a source you cite
    but don't physically hold) is honest, not every cited ref is a held document.
    """
    manifest_path = Path(manifest_path)
    bodies_dir = manifest_path.parent / "documents"
    docs: list[Document] = []
    if not manifest_path.exists():
        return docs
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        body_path = bodies_dir / f"{row['ref']}.txt"
        if not body_path.exists():
            continue
        docs.append(
            Document(
                ref=row["ref"],
                filename=row["filename"],
                gist=row.get("gist", ""),
                body=body_path.read_text(encoding="utf-8"),
                date=row.get("date"),
            )
        )
    return docs


class DocumentIndex:
    """Derived FTS5 index over held documents — content-addressable recall (decision #8).

    Indexes ``filename``, ``gist`` and ``body`` in one table so a single query ranks across all three
    (the production gap was filename-only matching). ``ref`` is stored ``UNINDEXED`` — retrievable, not
    matchable. Disposable by construction: the table is built from the document list and can be rebuilt.
    """

    def __init__(self, documents: list[Document]):
        self._docs: dict[str, Document] = {d.ref: d for d in documents}
        self._conn = sqlite3.connect(":memory:")
        self._build()

    @classmethod
    def from_manifest(cls, manifest_path: str | Path) -> "DocumentIndex":
        return cls(load_documents(manifest_path))

    def _build(self) -> None:
        self._conn.execute("DROP TABLE IF EXISTS documents")
        self._conn.execute(
            "CREATE VIRTUAL TABLE documents USING fts5("
            "ref UNINDEXED, filename, gist, body, tokenize='unicode61')"
        )
        self._conn.executemany(
            "INSERT INTO documents(ref, filename, gist, body) VALUES (?, ?, ?, ?)",
            [(d.ref, d.filename, d.gist, d.body) for d in self._docs.values()],
        )
        self._conn.commit()

    def documents(self) -> list[Document]:
        """All held documents, most-recent first (by manifest ``date``)."""
        return sorted(self._docs.values(), key=lambda d: (d.date or ""), reverse=True)

    def get(self, ref: str) -> Document | None:
        return self._docs.get(ref)

    def search(self, query: str, limit: int = 5) -> list[DocumentMatch]:
        """Content-aware document search over filename + gist + body, bm25-ranked (higher-is-better).

        The *same* standard FTS5 retrieval as the keyword path (#1), applied to the document substrate —
        the decision is the substrate and its passive surfacing (#8), not the search technology.
        """
        match = build_fts_query(query)
        if not match:
            return []
        cur = self._conn.execute(
            "SELECT ref, bm25(documents) AS rank FROM documents "
            "WHERE documents MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        )
        out: list[DocumentMatch] = []
        for ref, rank in cur.fetchall():
            doc = self._docs.get(ref)
            if doc is not None:
                out.append(DocumentMatch(document=doc, relevance=-rank))
        return out
