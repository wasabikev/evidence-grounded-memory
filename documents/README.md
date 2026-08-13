# documents/ — source-document recall (decision #8)

Uploaded source documents are part of the knowledge repository, not a sidecar — and **recalling and
referencing them is a first-class capability**, resolved the same way topic memory is: an inline citation
when memory has already drawn on a document, or an explicit content search when it hasn't. This layer makes
a held document's *content* discoverable and searchable on demand.

It is a **second substrate** alongside topic memory: where `memory/` holds *distilled* knowledge
(`topics/*.md`), this holds *primary-source* knowledge (the documents themselves). Both follow the same
thesis — the text is the source of truth, the index is derived and disposable.

| Module | Role | Decision |
|---|---|---|
| [`index.py`](index.py) | Derived FTS5 index over `filename + gist + body`; content-aware document search | #8 |

### What's genuinely new here (vs. "search, again")

The FTS5 retrieval is the *same standard technology* as the keyword path in #1 — reused deliberately, not
presented as novel. The decision is what surrounds it:

- **Documents as a first-class substrate**, with their citations (`[doc:e060·D]`, #4) resolving to
  recallable content rather than just a registry label. The production bug this originally fixed:
  filename-only matching meant a document whose answer lived in its *body* went unfound because its
  *name* didn't contain the query terms.
- **On-demand resolution, not a standing catalog.** An earlier version of this design pushed a per-agent
  inventory of every held document into every turn — measured, found redundant with the citation channel
  and a performance sink, and removed. See `docs/architecture.md` §8 for the full build → measure → remove
  story.

### Demonstrated vs. described

- **Demonstrated (runnable):** content recall — see the document-recall act in
  [`examples/demo.py`](../examples/demo.py), where a content query surfaces the quote document that a
  filename search would miss, and that document then drives the supersession in the re-grading act (#5).
- **Described (production property):** **per-agent isolation.** Production keys one index per agent so a
  document never surfaces for an agent that doesn't own it — structural isolation, not a remembered filter.
  This single-tenant reference doesn't exercise it; it's an assertion about deployment, not an algorithm.

### Fixtures: real design, illustrative data

Documents are committed as their *extracted text* (`examples/corpus/documents/<ref>.txt`) with metadata in
`examples/corpus/documents.jsonl`. This mirrors production's own split — an `UploadedFile` row's
`filename`/`ai_summary` (here: the manifest's `filename`/`gist`) versus its `processed_text` (here: the
`.txt` body). In production the body comes from OCR and the gist from a utility model; this offline,
zero-dependency demo commits both directly. The *design* is real; the *data* is illustrative.
