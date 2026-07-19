# iteration-store

Project-scoped long-term memory for coding agents.

A queryable store of durable facts about a codebase, so an agent iterating on a
project can carry knowledge across sessions without that knowledge living in
context. Each project gets its own database; nothing reaches across projects.

The design and its reasoning live in [DESIGN.md](DESIGN.md).

## What it optimizes for

**Avoiding re-derivation**, not reducing token usage in general. A query is only
worth making when the alternative was expensive rediscovery — reading several
files to re-establish something that was already worked out last week. A fact you
could get by reading one file is not worth storing.

## Status

Phases 1 and 2: the core library and the MCP server. Notes are not implemented
(phase 3).

## Use from Claude Code

```bash
claude mcp add iteration-store -- iteration-store-mcp
```

The server resolves the project from its working directory (git root, or the
directory itself). Set `ITERATION_STORE_PROJECT` to override, and
`ITERATION_STORE_HOME` to relocate the stores from `~/.iteration-store`.

Four tools: `remember`, `recall`, `confirm`, `revise`. A recalled fact carries any
reason to distrust it, with a diff as evidence:

```
#1 [gotcha] Rotation reverses the token in place; callers must not cache it.
    depends on: services/auth.py::rotate_token
    [!] deps_changed
        services/auth.py::rotate_token: changed since this was written
        @@ -1,6 +1,6 @@
         def rotate_token(t):
        -    return t[::-1]
        +    return t[::-1] + '!'
```

## Usage

```python
from datetime import timedelta
from iteration_store import Store

store = Store.open()   # resolves the project from the working directory

store.remember(
    "Token rotation reverses the token in place; callers must not cache it.",
    kind="behaviour",
    deps=["services/auth.py::rotate_token"],
)

for result in store.recall("token rotation"):
    print(result.memory.content)
    for suspicion in result.suspicions:
        print(f"  [{suspicion.reason.value}] {suspicion.evidence}")
```

A recalled memory carries any reason to distrust it rather than being hidden:

- `deps_changed` — the anchored span it depends on has moved, with a diff as
  evidence.
- `expired` — its review deadline has passed.

Both are repaired through the same path: `store.confirm(id)` if the fact still
holds, `store.revise(id, content=...)` if it does not.

### Anchors

A dependency can pin to a whole file (`services/auth.py`), a symbol
(`services/auth.py::rotate_token`), or a line span
(`services/auth.py::L40-L88`). Anchoring to a symbol is strongly preferred:
without it, any edit anywhere in the file makes the fact suspect, and in an
active repository everything goes stale within days.

## Development

```bash
poetry install
poetry run pytest
```

Tests use real SQLite databases and real temporary git repositories — no mocks.
SQLite is a sub-millisecond fixture, and the git behavior is exactly what needs
verifying.
