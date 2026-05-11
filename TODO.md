# TODO

Concrete cleanup, gaps, and unfinished work. CLAUDE.md captures vision; this file captures things to actually do.

---

## Codebase traps to resolve

Two files currently look authoritative on a quick scan but mislead anyone reading the code cold. Identified 2026-05-10.

### `pipeline/schema.sql` is dead but still loads at startup

It defines the old `tasks` / `agent_state` / `actions` / `artifacts` data model that's been superseded by `pipeline/schema_pipelines.sql` (pipelines → stages → jobs → dependencies → artifacts). Both are initialized at server startup; only `schema_pipelines.sql` is referenced by the live code.

**Resolve:** delete `pipeline/schema.sql` and remove its initialization from the server startup path. Grep tests, migrations, and seed scripts for references to the old tables first to be sure nothing depends on them.

### `harnesses/prompts/README.md` describes a protocol the code doesn't implement

The README documents agents emitting structured JSON like `{"reasoning": ..., "actions": [{"tool": "read_file", "args": {...}}]}`. The actual harnesses (`harnesses/agent.py`, `harnesses/harness_common.py`, `vendor_*.py`) route through vendor CLIs/APIs that return free-form text, which the harness then parses with `strip_thinking` / `extract_code`. The README describes a future direction, not current behavior.

**Resolve:** pick one of —
- Rewrite the README to describe current behavior, with a separate clearly marked "future direction" section, OR
- Keep it as a north-star spec but add a banner clarifying it's aspirational and pointing readers at the actual implementation files.
