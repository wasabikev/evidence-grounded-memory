# memory/ — the retrieval core

The runnable heart of the system. File-first storage, dual-path recall, and split-budget injection.

| Module | Role | Decision |
|---|---|---|
| [`store.py`](store.py) | File-first markdown store + section-level FTS5 keyword index, rebuilt from text | #1 (keyword path), #7 |
| [`semantic_index.py`](semantic_index.py) | Vector search layer over topic sections — the semantic half of dual-path recall | #1 (semantic path) |
| [`injector.py`](injector.py) | **Centerpiece.** Split-budget context assembly + the dual-path merge that reconciles keyword and semantic results | #1 (merge), #2 |
| [`consolidator.py`](consolidator.py) | Slim, non-destructive dedup + re-tier pass over topic bullets | #5 (async path), #6 |

**Source of truth invariant:** the markdown topic files are authoritative. Both indexes in this package
are *derived* from that text and can be deleted and rebuilt at any time without data loss.

See [docs/architecture.md](../docs/architecture.md) for the why.
