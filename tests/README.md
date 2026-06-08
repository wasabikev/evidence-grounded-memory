# tests/ — behavior-demonstrating tests

These tests exist to *demonstrate behavior*, not to chase production coverage. Each one should read as an
executable claim about a design decision:

- dual-path merge surfaces an exact-match term that semantic-only recall misses (#1)
- the split budget prevents a single large topic from starving cross-domain context (#2)
- a provisional `[user]` fact is promoted with `(confirmed by …)` when a higher-tier source corroborates,
  and superseded bidirectionally when one contradicts — without deleting the original (#5)
- section-level indexing injects a single section, not the whole topic (#7)

Run them with the stdlib test runner (no third-party dependencies):

```bash
python -m unittest discover -s tests -p "test_*.py"
```

See [`test_memory.py`](test_memory.py) (decisions #1, #2, #5, #6, #7) and
[`test_evidence.py`](test_evidence.py) (decisions #3, #4).
