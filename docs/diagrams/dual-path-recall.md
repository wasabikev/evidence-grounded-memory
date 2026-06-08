# Dual-path recall (decision #1)

The markdown topic files are the source of truth; the keyword (FTS5) and semantic (vector) indexes are
**derived and disposable**, rebuilt from that text. A query runs *both* paths in parallel, and a merge
layer reconciles their two rankings. Neither path alone is sufficient — exact technical terms come from
the keyword path, conceptual proximity from the semantic path — so the **merge** is the point.

The merge is **Reciprocal Rank Fusion**: it combines the two *rankings* rather than raw scores, so no
cross-path score calibration is needed. (Production uses tuned score-fusion weights; those weights are the
withheld calibration — RRF is the clean, re-derivable default published here.)

```mermaid
flowchart TD
    Q(["query"])
    MD[("topics/*.md<br/>(source of truth)")]
    FTS["FTS5 section index<br/>keyword · bm25"]
    VEC["semantic index<br/>section vectors · cosine"]
    RRF{{"dual-path merge<br/>Reciprocal Rank Fusion<br/>+ per-path thresholds"}}
    MERGED["merged ranked sections"]

    MD -->|"rebuild (derived, disposable)"| FTS
    MD -->|"rebuild (derived, disposable)"| VEC
    Q --> FTS
    Q --> VEC
    FTS -->|"ranked by bm25"| RRF
    VEC -->|"ranked by cosine"| RRF
    RRF --> MERGED
```

Implementation: [`memory/store.py`](../../memory/store.py) (keyword path),
[`memory/semantic_index.py`](../../memory/semantic_index.py) (semantic path),
[`memory/injector.py`](../../memory/injector.py) (`merge_dual_path`).
