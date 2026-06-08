# Diagrams

Three Mermaid diagrams of the load-bearing decisions. They render natively on GitHub and are plain text,
so the design is legible to both a human skim and an automated reader.

| Diagram | Decision(s) | What it shows |
|---|---|---|
| [dual-path-recall.md](dual-path-recall.md) | #1 | Markdown source of truth → derived keyword + semantic indexes → RRF merge |
| [budget-split.md](budget-split.md) | #2, #7 | Merged sections packed into a partitioned budget: shared pool + separate INDEX cap |
| [tier-flow.md](tier-flow.md) | #3, #4, #5 | The authority ladder and a fact's lifecycle: provisional → promoted / superseded |

These are embedded inline at the relevant decisions in [../architecture.md](../architecture.md).
