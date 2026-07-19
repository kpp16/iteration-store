# iteration-store — Design

Status: agreed direction, pre-implementation. Records decisions made so far and the
reasoning behind them, so they don't get relitigated or silently reversed.

## Purpose

A long-term memory store for Claude Code, scoped to a single project, queried over MCP.
The name refers to iterating on an application over time: the store is what lets a coding
agent carry knowledge across sessions without that knowledge living in context.

## What this optimizes for

**Avoiding re-derivation** — not reducing token usage in general.

This distinction drives the rest of the design. A query round-trip is not free: it costs a
tool call, results, and reasoning over those results. The store only wins where the
alternative was expensive rediscovery — reading several files and re-tracing a call chain
to work out something that was already worked out last week. Where the alternative was
already cheap, the store is pure overhead.

Consequences:

- A fact derivable by reading one file is not worth storing.
- Success is measured by re-derivation avoided, not by bytes stored or context saved.
- Growth of the store is not itself a good sign.

## Non-goals

- **Cross-project memory.** Nothing in one project's store is reachable from another.
  A later user-facing tool may query across all stores; that surface is deliberately not
  exposed over MCP. Much later.
- **Team-shared memory.** Memory that travels with a clone is a real feature but a much
  larger design (merge conflicts, trust, PII). Not paid for now.
- **Being a vector database.** See Retrieval.

## Architecture

- **Transport:** MCP server, so the store is a first-class tool rather than something
  shelled out to.
- **Storage:** SQLite, one database file per project.
- **Retrieval:** FTS5 (keyword) plus structured filters, to start.
- **Language:** Python (project already uses poetry, and the MCP Python SDK fits).

### Retrieval: why not embeddings first

Semantic similarity is mediocre for code facts — a query like "how does auth work" often
does not embed near a stored note about token rotation. Hybrid keyword plus structured
filtering is a stronger baseline here and far simpler to operate. Embeddings get added
only once FTS5 is demonstrably failing on real queries, with those queries as evidence.

## Isolation: one DB per project

**Decision:** one SQLite file per project, not a shared database with a `project_id`
column.

**Why:** isolation becomes structural rather than a `WHERE` clause that has to be correct
on every query path. With an MCP server in the loop, a forgotten filter means one
project's memory leaks into another project's context — the worst failure this system
could have. Separate files make that impossible rather than merely unlikely.

The eventual cross-project tool stays easy: a registry of project → DB path, queried in
sequence and merged. Global queries are rare and human-driven, so the fan-out cost is
irrelevant.

### Storage layout

```
~/.iteration-store/
  registry.db            -- project key -> store path
  <key>/store.db
```

Central rather than in-repo: the registry is needed anyway for the later global tool, and
this keeps user repos clean with no gitignore trap.

### Project identity

- `git rev-parse --show-toplevel` when inside a git repo.
- Resolved cwd otherwise.
- **Worktrees share the main repo's store** — a worktree is the same project mid-iteration,
  which is exactly the use case this serves.

## Memory units and validation

A memory is a piece of content plus a strategy for deciding whether it is still true.

```
memory
  id, content, kind
  created_at, last_confirmed_at
  write_commit           -- HEAD at write time
  validation_strategy    -- 'deps' | 'decay' | 'manual'
  review_interval        -- cadence for 'decay'; NULL = never expires
  next_review_at         -- deadline; pushed out by review_interval on confirm
  last_recalled_at       -- usage signal; recorded now, consumed later
  recall_count           -- usage signal; recorded now, consumed later

memory_dep
  memory_id, path
  anchor                 -- 'services/auth.py::rotate_token' | 'L40-L88' | NULL (whole file)
  blob_sha, span_sha
```

### Staleness is the central problem

A stale fact is worse than a missing one. "Auth lives in `services/auth.py`" becomes
actively harmful the day that file moves — confidently wrong, and acted upon. Whether
invalidation works decides whether this project is useful or a liability.

### Anchors, not whole files

Hashing whole files is too coarse. Any edit to a large file would invalidate every fact
touching it; in an active repo the entire store goes suspect within days, and it fails not
by being wrong but by never trusting itself.

So each dependency carries an **anchor** — a symbol or line span — alongside the path. On a
file-hash mismatch, the anchor is re-resolved and only that span is hashed; if the function
body is unchanged, the fact survives.

First pass can resolve symbols with a regex-based finder. tree-sitter later, if the regex
approach proves too lossy.

### Hashes: git blob SHA, plus the commit

- Prefer the **git blob SHA** — git computes it already (`git hash-object`), so it is free.
- Store **`HEAD` at write time**. This is the real payoff: on a mismatch,
  `git diff <write_commit> HEAD -- <path>` shows exactly what changed, turning "this might
  be stale" into "this changed in a way that does / does not touch the fact." Much cheaper
  revalidation than re-reading the file.
- **sha256 fallback** for untracked or non-git files.

### Validity is not binary

Most edits to a file do not falsify a fact about it. A hash mismatch means **suspect**, not
invalid.

- `recall` returns suspect facts *with* a staleness flag, a **reason**, and any relevant
  diff, and lets the caller judge.
- `confirm` and `revise` operations repair a suspect fact in place.

Suspicion has more than one cause, and they share a single path:

| reason | trigger | evidence returned |
| --- | --- | --- |
| `deps_changed` | anchor hash mismatch | `git diff <write_commit> HEAD -- <path>` |
| `expired` | `next_review_at` passed | age since `last_confirmed_at` |

A fact can be suspect for both at once. One flag and one repair path means the agent learns
a single mechanism, and new validation strategies do not each invent their own protocol.

### TTL / review cadence

For facts that no file hash can validate — decisions, preferences, conventions — expiry is
the only lever that surfaces silent reversal. `next_review_at` passing makes the fact
suspect; `confirm` pushes it out by `review_interval`, `revise` rewrites and resets it.

Two cautions:

- **`NULL` must mean never.** Plenty of facts are genuinely durable, and forcing periodic
  review on all of them is churn with no signal.
- **Default intervals should be generous.** Expiry fires on a schedule whether or not
  anything actually changed. Too short, and everything is permanently suspect, the flag
  becomes noise, and agents learn to ignore it — the same failure mode as hashing whole
  files, arrived at from the other direction.

The name is `next_review_at` rather than `next_update`: most reviews should end in
confirmation, not rewriting, and the name should not imply an obligation to change
something.

This is the flywheel: the store heals through use. A store that can only delete converges
to empty.

### Write path: the tool description, and nothing else

**Decision:** `remember` applies no programmatic gatekeeping. What gets stored is governed
entirely by the MCP tool description.

This is the cheapest option and not a naive one. The description is the only thing an agent
reads when deciding whether to store something, which makes it the highest-leverage surface
in the system — worth real drafting effort in phase 2, and worth revising as failure modes
show up. Rejection logic layered on top would be guessing at a problem whose shape has not
been observed yet.

The tradeoff, stated plainly: **there is no backstop.** If the description under-constrains,
the store fills with restatements of the code, and bad writes cannot be repaired downstream
— retrieval can rank and filter but cannot manufacture signal that was never there.

What makes this acceptable for now:

- Notes exist, so low-value material has somewhere to go that is not the curated table.
- `recall_count` and `last_recalled_at` are being recorded, so the question becomes
  empirical: facts that are never retrieved are the evidence for whatever constraint comes
  later.

**Revisit when** usage data shows a meaningful fraction of stored facts are never recalled,
or recall results are visibly diluted by near-duplicates.

### Cleanup / forcing function (deferred — record usage now)

**Not implemented in the first version.** But the problem is real and one piece of it
cannot be deferred.

The gap: a fact goes suspect, no agent ever confirms or revises it, and it stays flagged
forever. Indefinite suspicion is the safe default — nothing true is ever silently
destroyed — but with no forcing function the store slowly fills with permanently-flagged
clutter that every recall has to wade through. Suspicion with no consequence eventually
reads as noise, which is the same way a too-short TTL fails.

Sketch of the eventual policy, to be argued later:

- **Archive, never delete.** A fact that stays unconfirmed past some threshold moves out of
  the default recall path but remains recoverable. Silent automatic deletion of something
  that may well still be true is the one outcome to rule out now.
- **Combine suspicion with disuse.** Neither signal is strong alone: a suspect fact may
  simply be durable and rarely relevant, and an unused fact may be waiting for the right
  task. Together — flagged *and* never recalled in a long window — they are a genuinely
  good archive candidate.
- **Repair on read is the real forcing function.** The background job is a backstop. What
  actually keeps the store healthy is agents encountering suspect facts during normal work
  and confirming or revising them, which is why `confirm`/`revise` must stay cheap enough
  that fixing one is easier than ignoring it.
- **Review belongs to the human surface.** Bulk archival decisions fit the later
  user-facing cross-project tool far better than an MCP call an agent makes mid-task.

**What cannot wait:** any sensible policy needs usage data, and usage data is
unbackfillable — exactly the argument for capturing note provenance up front. So
`last_recalled_at` and `recall_count` get written from day one and consumed whenever this
is taken up. Writing them costs one UPDATE per recall; not writing them means the cleanup
design starts blind whenever it starts.

### Facts with no file dependencies

"We chose Postgres over Mongo because X", "user prefers no comments in generated code" —
these can never go stale under dependency hashing, yet they are exactly the facts that go
stale *silently* when a decision is reversed. They need different levers:

- **Explicit supersession** — a new fact marks an older one dead.
- **Age-based decay.**

Hence `validation_strategy` as a per-memory field, rather than baking dependency hashing in
as the universal mechanism.

## Notes: the low-bar table

A deliberately open-ended table where Claude Code, agents, and subagents write notes,
reasoning, observations, and dead ends. Free to use, no curation bar, no schema for the
content itself.

```
note
  id, body
  author                 -- 'claude-code' | agent or subagent name
  session_id
  created_at
  cwd_commit             -- HEAD when written, if in a repo
  paths                  -- files in play at write time, if any (advisory, not validated)
```

Same database, so it inherits project isolation. Indexed with FTS5 from the start.

### Scope for now: write-only

**`recall` does not search notes.** Notes are a separate surface — logging and storage
only — with their own write path and, for now, no read path exposed to agents. Integrating
them into retrieval is a later conversation.

This keeps the first version honest. Curated memory retrieval can be evaluated on its own
terms without unfiltered chatter diluting the results, and the notes corpus can accumulate
real traffic before anyone designs queries against a guess about its shape.

### Why this exists

It is the counterweight to `remember`'s high bar. `memory` deliberately rejects anything
derivable from reading one file — correct, but it leaves valuable material homeless.
Dead ends are the clearest case: "tried X, it fails because Y" pins to no file hash and
states no durable fact, yet it is exactly what saves an agent an hour six weeks later.

Without a low-bar table there is constant pressure to loosen the high-bar one. Better to
have two tables with honest, different standards than one table with a standard nobody
holds to.

### Provenance is not deferred

How to query notes well is genuinely open and can be settled later. **Provenance cannot
be.** Author, session, timestamp, and commit are unrecoverable after the fact, and every
plausible retrieval strategy — by session, by agent, by recency, by what changed since —
needs some of them. Cheap to write now, impossible to backfill.

For the same reason, FTS5 indexing goes on from day one: one virtual table, and the notes
are never *unqueryable* while the querying question stays open.

### Known risk

The realistic failure mode is a write-only log — heavy writes, no reads, unbounded growth,
no signal. Nothing to build against that yet, but the provenance columns are what make
retention, pruning, or ranking possible when it does become a problem.

A promotion path (a note that keeps proving useful is rewritten as a curated `memory`) is
the obvious eventual bridge between the two tables. Not now.

## Notes retrieval (deferred — not being built)

Notes are write-only for now (see above). This section is preserved thinking for when
retrieval is taken up, not a plan for the current version. Measure it against real traffic
before building any of it; the weights especially are guesses.

### Framing

At scale the notes problem is **relevance decay**, not semantic precision. Most notes are
meaningful only within a narrow window of time and code. In a corpus of tens of thousands,
pure semantic search returns results that are semantically excellent and contextually dead
— a note about a module deleted months ago, a dead end that stopped being one when a
dependency was upgraded. Better embeddings do not fix that; they retrieve stale content
more confidently.

So: rank on structural context first, semantics second.

### Signals

Blended into a single score:

1. **Path overlap** — the strongest single signal, and the reason `paths` is recorded. A
   note tagged `services/auth.py` is likely relevant to an agent editing that file whatever
   its wording. Cheap join, beats embeddings on the common case.
2. **FTS5 / BM25** over the body — already present, free.
3. **Recency decay** — notes age considerably faster than curated facts.
4. **Session affinity** — notes from the current session are working memory and should rank
   high; a subagent should see what its parent just observed.
5. **Churn penalty** — using `cwd_commit` and `paths`, measure how much those paths have
   changed since the note was written. Heavy churn means the note describes a world that no
   longer exists. This is the notes analogue of memory staleness, reusing the same git
   plumbing, without pretending notes are verifiable facts.

Linear blend to start, weights tuned against real queries.

### On embeddings / RAG

Second, not rejected. There is a real case FTS5 cannot be tuned into handling: a conceptual
query sharing no vocabulary with the note — "why is login slow" against a note reading
"N+1 in the session lookup". BM25 has nothing to work with there.

When added, embeddings should rank **within a structurally filtered candidate set**, not
search the whole corpus. They complement path filtering rather than replacing it.

### Compaction before RAG

If notes explode, the first move is compaction, not better search over an ever-growing pile.
Notes from old sessions about resolved problems can be collapsed — many into one summary,
or promoted into a curated `memory` where a durable fact fell out of them.

This attacks size at the source, keeps the corpus dense, improves every retrieval strategy
including the eventual embeddings, and supplies a retention story in place of unbounded
growth.

### Response shape

`recall` over notes returns **snippets with an expand call**, not full bodies. A wide match
returning full text would blow the context budget this system exists to protect.

## Open questions

- What the `kind` taxonomy should be, if any beyond free text.
- Whether `remember` should surface near-duplicates in its *response* (not reject them) —
  the one gap a tool description cannot close, since an agent cannot avoid re-storing what
  it does not know is already there.
- How supersession is decided: explicit call, or inferred at write time.
- Whether decay should surface as suspicion (like dep mismatch) or as ranking penalty.
- Default `review_interval` per kind of fact, and whether the agent sets it at write time or
  it is inferred. Getting this wrong in the short direction makes the suspect flag noise.
Deferred, not live questions:

- Archive thresholds, and whether archival is ever automatic or always human-confirmed
  (see Cleanup / forcing function).

- Weights for the notes scoring blend.
- What triggers compaction: size threshold, session age, or an explicit call.
- Whether the churn penalty is worth its cost, given it needs git work per candidate. May
  need to run only on the top-N after cheaper signals filter.

## Implementation phases

Ordering principle: **core library first, MCP second.** Every behavior worth testing —
hashing, anchor re-resolution, expiry, suspect reasons — is a direct function call in a
library and a transport round-trip through a server. Building the store as a plain Python
package with a clean API, then wrapping it, is what makes the testing below practical
rather than aspirational.

### Phase 1 — core store (no MCP) ✅ implemented

The walking skeleton, as a library.

- SQLite schema: `memory`, `memory_dep`, FTS5 index. Migrations from the start, even
  trivially — the schema will move.
- Project resolution: git toplevel, cwd fallback, worktrees to the main store, registry
  entry, `~/.iteration-store/<key>/store.db` layout.
- `remember` / `recall` / `confirm` / `revise` as library calls.
- Git plumbing: blob SHA, sha256 fallback, `write_commit` capture, diff on mismatch.
- Anchor resolution — regex symbol finder plus line spans; span hashing.
- Validation with both signals: `deps_changed` and `expired`, shared suspect path, reason
  and evidence on every recall result.
- `last_recalled_at` / `recall_count` written.

*Done when:* a fact can be stored against an anchor, recalled clean, comes back suspect
with a diff after an unrelated-file edit is correctly *not* flagged and a real edit to the
anchored span *is*, and comes back suspect with an age after its review deadline passes.

### Phase 2 — MCP server ✅ implemented

- Wrap the phase-1 API in MCP tools; no new store logic.
- Tool descriptions are real design work, not boilerplate: they are the entire interface an
  agent has for deciding what to store and when. Expect to iterate on wording.
- Manual end-to-end use from Claude Code against a real project.

*Done when:* Claude Code can remember and recall against a scratch project over MCP, and a
suspect result visibly prompts a confirm or revise.

### Phase 3 — notes ✅ implemented

Small, and only useful once MCP exists, since agents are the only writers.

- `note` table, FTS5 index, provenance columns, write path exposed over MCP.
- No read path. `recall` remains memory-only.

*Done when:* agents and subagents can log notes that land with correct author, session, and
commit provenance.

**Deviation from the sketch:** paths are a `note_path` child table rather than a column.
The deferred retrieval design leans on path overlap as its strongest signal, and a child
table makes that an indexed join instead of a migration. Same argument as recording
provenance early — cheap now, awkward later.

### Concurrency ✅ implemented (pulled forward from phase 4)

Multiple agents means multiple processes against one file, so this could not wait for a
later hardening pass.

- **WAL** — readers proceed while a writer holds the lock. Persisted in the database
  header, so it is set once and inherited by every later connection.
- **`busy_timeout` (5s)** — the critical one. Without it a competing writer fails instantly
  with "database is locked" rather than waiting out a write that takes microseconds.
  Verified by mutation: at zero, every multi-process test fails.
- **`synchronous = NORMAL`** — safe under WAL (a crash can lose the last transaction but
  cannot corrupt the file) and avoids an fsync per write.
- **Migrations run inside `BEGIN EXCLUSIVE`**, re-reading the version *inside* the lock.
  Found by test: two agents starting simultaneously both saw `user_version == 0` and both
  tried to create the schema, and the loser failed with "table memory already exists". This
  is why migrations are tuples of statements — `executescript` issues an implicit COMMIT
  that would dissolve the transaction, and splitting on `;` is impossible with trigger
  bodies.
- **A per-Store lock** for the second axis: one connection shared across threads, which
  SQLite does not serialize. Cheap insurance for a threaded host; the cross-process path
  does not depend on it.

Known limit: WAL requires a local filesystem. A store on NFS or a network share will
misbehave, and nothing detects that.

### Phase 4 — hardening

Driven by whatever the first three phases actually expose.

- tree-sitter anchors if the regex finder proves too lossy.
- Recall performance and result-shaping at realistic corpus sizes.
- Tuning defaults for `review_interval` per kind.

### Later

Everything already marked deferred: notes retrieval, compaction, cleanup and archival,
the cross-project user-facing tool.

## Testing

Unit tests from phase 1, alongside the code rather than after it. pytest.

**APIs are not designed around the test suite.** No injected clocks in public signatures, no
DI machinery, no interfaces existing only for mocking. Modularize where it pays, not by
rule.

Two light conventions that follow from ordinary good structure rather than testability:

- **Decision functions take their inputs.** `determine_suspicion(dep_states, next_review_at,
  now)` receives the current time rather than calling a global inside itself; the store
  method calls `datetime.now()` and passes it down. Public API is unchanged and no caller
  sees a difference.
- **Anchor resolution stays a string operation.** `find_span(source, anchor)` takes text and
  returns a span, with file reading handled by the caller. This is the natural shape anyway.

**Do not mock the database.** SQLite in-memory (`:memory:`) or a tmp file is a
sub-millisecond fixture — there is no slow dependency to escape, and writing mocks would be
more work that tests less. The SQL *is* the behavior for FTS5 matching, the
memory↔memory_dep joins, and migrations; a mock there only asserts that the mock returns
what it was told to. Use real stores against real temporary repos.

**What the tests actually need to cover** — the invalidation logic is the whole value of the
project, so it gets the most coverage:

- *Git fixtures.* Real temporary repositories: init, commit, mutate, commit again. Blob SHA
  capture, diff generation, and untracked-file sha256 fallback all need genuine git state,
  not mocks. Build this fixture well and early; most other tests lean on it.
- *Anchor precision, both directions.* An edit elsewhere in a dependency file must **not**
  flag the fact — the false-invalidation failure the anchor design exists to prevent, and
  the easiest thing to silently regress. An edit inside the anchored span must flag it.
- *Anchor loss.* The symbol is renamed, moved to another file, or deleted. This is where a
  regex finder will be weakest, so the tests should pin down the intended behavior rather
  than whatever it happens to do.
- *Expiry.* **Deferred for now** — deliberately, not by oversight. Worth revisiting because
  expiry is the only validation signal for facts with no file to hash (decisions,
  preferences, conventions), and time logic fails silently: naive vs aware datetimes, or an
  inverted comparison that leaves everything or nothing suspect. Neither is visible without
  a test, and either makes the flag worthless.
- *Combined suspicion.* Both reasons at once, and clearing one leaves the other.
- *Project isolation.* Two projects, two stores, no leakage — including the worktree case
  resolving to the main repo's store.
- *Migrations.* A store created at an older schema version opens and upgrades cleanly.
