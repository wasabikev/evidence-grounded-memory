# evidence/ — the authority / provenance layer

Every fact in memory carries where it came from and how much to trust it. This layer supplies the
vocabulary (tiers + categories), the inline tag format, and the registry that resolves tags back to full
source metadata. It is **interwoven with** `memory/`, not a standalone module — provenance is a property
of every stored fact, not a sidecar.

| Module | Role | Decision |
|---|---|---|
| [`tiers.py`](tiers.py) | The 7-tier / 14-category authority vocabulary + resolver; tier → trust/badge mapping | #3 |
| [`sources.py`](sources.py) | `sources.jsonl` registry schema + the inline-tag parser that resolves `[doc:e037·A]` → full source record | #4 |

### Tier sketch (illustrative)

The published vocabulary is the *skeleton*; the domain-calibrated recognition cues that classify a source
into a category are withheld (see the publication boundary in
[docs/architecture.md](../docs/architecture.md#publication-boundary)).

| Tier | Kind of source (demo domain) | Inline tag example |
|---|---|---|
| A | Authoritative regulation (building code) | `[doc:e037·A]` |
| B | Official guidance (permit office) | `[doc:e041·B]` |
| C | Professional standard / licensed expert | `[doc:e055·C]` |
| D | First-party document (contractor quote) | `[doc:e060·D]` |
| E | Aggregated third-party (product reviews) | `[web:e072·E]` |
| F | User statement | `[user]` |
| G | Model inference | `[ai]` |

Only `[doc]` / `[web]` tags carry a source ref + tier; `[user]` and `[ai]` are tier-fixed by kind.

See [docs/architecture.md](../docs/architecture.md) for tier semantics, conflict resolution, and the
temporal re-grading (promotion / supersession) that makes these tiers *dynamic*.
