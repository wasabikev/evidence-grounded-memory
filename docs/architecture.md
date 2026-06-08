# Architecture — evidence-grounded-memory

**Status:** Active
**Scope:** memory retrieval + provenance/evidence layer

## Purpose

The long-form design narrative behind the reference implementation: the problem framing, the seven
design decisions, and the boundary between what this repo publishes and what it deliberately withholds.
The [repo README](../README.md) is the compressed read; this is the depth.

---

## The problem

"Agent memory" is usually shorthand for a vector database: embed every chunk, retrieve top-k by cosine
similarity, paste into the prompt. That framing answers one narrow question — *which text is semantically
nearest the query* — and quietly assumes away the problems that actually make persistent memory hard at
production scale. Four of them:

### 1. What *is* the memory? (substrate)

A vector index is opaque, lossy, and unauditable — you can't read it, diff it, or hand a topic to a human
and say "this is what the agent knows about you." This system treats durable, human-readable **markdown
topic files** (`topics/*.md`, H2-sectioned) as the *source of truth*, and every index — keyword and
semantic — as a **derived, disposable view** rebuilt from that text. Semantic search is a retrieval
path, **not** the memory system. This file-first stance is the foundation the other three problems are
solved on top of.

### 2. One retrieval path is never enough (plural retrieval)

Pure semantic search misses exact-match technical terms (a permit code, a person's name); pure keyword
search misses conceptual proximity. You need both running in parallel with a merge layer that reconciles
them — and it must inject only the relevant *section* of a topic, inside a fixed token budget that no
single topic is allowed to monopolize.

### 3. Not all facts deserve equal trust (provenance)

A claim from a statute, from a forum post, from the user directly, and from the model's own inference
cannot be weighted the same. Memory needs provenance — every fact carries where it came from — and an
authority model that governs how conflicts resolve and when output is flagged for verification.

### 4. Authority is temporal, not fixed at write time (temporal authority)

This is the part the vector-DB framing has no answer for. Facts arrive at different times from sources of
different strength, and their standing *changes as evidence accumulates*. A fact can enter the store as a
low-tier user statement or AI inference, then be **promoted** when a higher-authority document later
corroborates it — or **superseded** when an authoritative source contradicts it. Memory is therefore a
*living evidentiary record*: provisional on entry, continuously re-graded, and held coherent over time by
a scheduled consolidation pass that dedups, reconciles, and re-tiers. Without that, the corpus drifts
into contradiction and stale confidence.

These four problems map directly onto the seven design decisions below.

---

## The architecture

**File-first.** Human-readable markdown topic files are the source of truth; the keyword (FTS5) and
semantic (vector) indexes are derived views rebuilt from that text.

**Dual-path recall.** Both indexes run in parallel and a merge layer reconciles their results.

**Split-budget passive injection.** The relevant *sections* are assembled into the prompt before each
call, within a fixed, partitioned token budget.

**Interwoven provenance layer.** Every fact is tagged with its source and authority tier.

**Scheduled consolidation.** Because authority is temporal, a scheduled pass re-grades the corpus over
time — promoting corroborated facts, marking superseded ones, deduplicating, and keeping tiers honest.

---

## Key design decisions

### 1. Dual-path recall (keyword + semantic, merged)

FTS5 keyword index and semantic vector search run in parallel, then a merge/ranking layer combines them.
Pure semantic search misses exact-match technical terms; pure keyword search misses conceptual proximity.
The interview point is the *merge*, not either path alone.

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

The reference merge is **Reciprocal Rank Fusion** — it combines the two *rankings*, not raw scores, so no
cross-path score calibration is needed. Production uses tuned score-fusion weights; those are the withheld
calibration. (Diagram: [diagrams/dual-path-recall.md](diagrams/dual-path-recall.md).)

### 2. Split-budget context injection

A fixed injection budget split between a shared pool (~2K tokens) and a separate INDEX cap (~2.1K).
Prevents any single topic from crowding out cross-domain context. A deliberate architectural constraint,
not a default.

```mermaid
flowchart TD
    MERGED["merged ranked sections"]
    PACK{{"split budget"}}
    POOL["recalled sections<br/>(greedy section snippets)"]
    IDX["INDEX<br/>compressed table of contents<br/>(every topic)"]
    CTX["injected context block"]

    MERGED --> PACK
    PACK -->|"shared pool ≤ 2000 tok"| POOL
    PACK -->|"INDEX cap ≤ 2100 tok"| IDX
    POOL --> CTX
    IDX --> CTX
```

Because the shared pool and the INDEX have *independent* caps, a single large or highly-relevant topic
can fill the pool without starving the cross-domain map. (Diagram:
[diagrams/budget-split.md](diagrams/budget-split.md).)

> *Production note:* the production system also injects CORE profile, tasks, session summaries, and a
> temporal summary. The reference version simplifies to the shared-pool / INDEX split to keep the story
> clean. The illustrative token allocations here are re-derivable defaults, not tuned production values.

### 3. Authority-tiered provenance (the evidence layer)

A 7-tier / 14-category source vocabulary. Every memory bullet carries an inline tag (`[doc:e037·A]`,
`[user]`, `[ai]`). The tier governs how conflicting facts are weighted and whether output is flagged for
verification. This is **interwoven with the memory system, not a separate module** — which is why
`evidence/` and `memory/` live in one repo.

### 4. Sources registry

A canonical registry of all ingested documents (`sources.jsonl`). Inline provenance tags in memory
bullets resolve back to this registry for full metadata (label, category, tier, recency, scope-fit).

### 5. Temporal authority — promotion & supersession

**The decision the vector-DB framing has no analog for, and the strongest single signal of original
systems thinking.** Authority is not stamped once at write time; a fact's standing evolves as evidence
accumulates. The system makes that evolution *first-class and auditable* rather than destructive:

- **Provisional on entry.** A fact written from a user statement (`[user]`, Tier F) or model inference
  (`[ai]`, Tier G) enters at low authority — it is recorded, not trusted.
- **Promotion via corroboration.** When a higher-authority source later confirms the same claim, the
  bullet gains an inline `(confirmed by <ref>)` annotation — its effective trust rises without rewriting
  history.
- **Supersession, recorded bidirectionally.** When an authoritative source contradicts an existing
  bullet, the system writes a *paired* annotation: the new bullet gets `(supersedes …)` and the old
  bullet gets `(superseded YYYY-MM-DD by <ref>)`. The original is never deleted — the audit trail of
  *what the agent believed and why it changed* is preserved.
- **Two trigger points, same record.** Re-grading happens **synchronously** (in-conversation: when a
  higher-tier source enters the chat, the agent reconciles it against injected memory and updates in the
  same turn — firing on the evidence, informing the user rather than asking) **and asynchronously** (the
  scheduled consolidator backstops runtime omissions and pre-feature corrections).

```mermaid
flowchart TD
    subgraph LADDER["authority tiers — strongest → weakest"]
      direction LR
      A["A · regulation"] --> B["B · official guidance"] --> C["C · professional standard"] --> D["D · first-party doc"] --> E["E · aggregated 3rd-party"] --> F["F · user"] --> G["G · model"]
    end

    NEW(["new fact"])
    PROV["provisional bullet<br/>[user] F · [ai] G"]
    CONF["promoted<br/>(confirmed by &lt;ref&gt;)"]
    SUP["superseded — bidirectional, non-destructive<br/>old: (superseded &lt;date&gt; by &lt;ref&gt;) — kept<br/>new: (supersedes …)"]
    REG[("sources.jsonl<br/>registry")]

    NEW --> PROV
    PROV -->|"higher-tier source corroborates"| CONF
    PROV -->|"higher-tier source contradicts"| SUP
    CONF -->|"later contradiction"| SUP
    REG -.->|"tag → tier resolution"| PROV
    SYNC["trigger · sync<br/>in-conversation"] --> PROV
    ASYNC["trigger · async<br/>scheduled consolidator"] --> PROV
```

(Diagram: [diagrams/tier-flow.md](diagrams/tier-flow.md).)

The interview point: *memory is an evidentiary record with a changelog, not a key-value store.* Trust is
a function of corroboration over time, and the system encodes that as durable, human-readable annotations
rather than silent overwrites. Decisions #3 (tiers) and #4 (sources) supply the vocabulary; this decision
is what makes them *dynamic*. Decision #6 is one of its two execution paths.

### 6. Scheduled consolidation

A scheduled agent pass that merges, deduplicates, and re-tiers memory topics — keeping the knowledge base
coherent over time without manual curation. It is the *asynchronous* execution path for the temporal
re-grading in #5 (catching corroboration/supersession the runtime missed) plus the mechanism that
prevents corpus drift. Non-destructive, idempotent, budget-aware.

> *Reference scope:* the runnable consolidator here is deliberately slim — bullet dedup + re-tier via the
> sources registry. Task archival, profile dedup, and topic-reorg overflow handling exist in production
> and are described-only.

### 7. Section-level indexing

Topics are H2-sectioned markdown; the FTS5 index operates at the *section* level, not the document level.
This enables budget-aware injection of just the relevant section rather than a whole topic — the
mechanism that makes decision #2 possible.

---

## Publication boundary

This repo distinguishes **sanitization** (mechanically scrubbing client/infra/tenancy specifics) from the
**publication boundary** (a strategic call about how much *design depth* to publish per decision).

**Governing principle:** *publish the what and why; withhold the calibration.* The architecture and its
rationale are the asset and cost little to share. The **tuning** — vocabularies calibrated over real
engagements, production prompts, thresholds, ranking weights, and eval results — is the moat. A reference
implementation should *demonstrate* each decision, not ship the production-tuned artifact.

**Standing rules:**

- **No production prompts.** Extraction, consolidation, and reconciliation system prompts are withheld;
  reference modules use minimal illustrative prompts.
- **No eval results or tuning data.** No benchmark numbers, thresholds chosen from real data, or ranking
  weights presented as production values (illustrative defaults are fine, labeled as such).
- **The repo is a hook, not a spec.** When in doubt, publish the principle and a clean demo; leave the
  depth for the interview.

| # | Decision | Publish (the asset) | Withhold (the moat) | Sensitivity |
|---|---|---|---|---|
| 1 | Dual-path recall | The merge concept + a working keyword/semantic merge in the neutral domain | Production ranking weights / score-fusion tuned on real data | Low |
| 2 | Split-budget injection | The split principle + partition rationale; illustrative token allocations | — (defaults are re-derivable) | Low |
| 3 | Authority-tiered provenance | Tag format, the 7-tier skeleton, the *concept* of MECE categories | The tuned 14-category cue table (domain-calibrated recognition cues) — generic version only | **High** |
| 4 | Sources registry | Schema shape + tag→source resolution flow | Production enrichment heuristics (independence/recency/scope-fit scoring) | Med |
| 5 | Temporal re-grading | The concept, annotation format, the two-trigger design | Production reconciliation prompts + promotion/supersession heuristics + thresholds | **High** |
| 6 | Scheduled consolidation | The slim dedup + re-tier loop; non-destructive / idempotent / budget-aware principles | Production merge prompts + LLM-judgment heuristics; the full sub-task set | Med |
| 7 | Section-level indexing | Fully — standard technique, no calibration to protect | — | Low |

The two **High** rows (#3 cue table, #5 mechanism) are the crux of "are we publishing too much?" The
handling: publish the principle and shape at full strength (they are the strongest signals); withhold the
calibrated cue table and the production prompts/thresholds. Publishing #5 is treated as deliberate,
irreversible prior art — not a default.
