# Security audit — Round 3: multi-agent recruitment consensus

**Date:** 2026-07-24
**Method:** static/code-path analysis against the real transaction bodies in
`ops/recruitment.py`, `ops/authority.py`, `ops/orphans.py`, and the two
Lambdas — no disposable CockroachDB cluster was available in this session
(no `docker`/`cockroach` in the sandbox), so this round is analysis plus pure
unit coverage, not adversarial induction like Round 2. The two contained
fixes below should still be exercised against a real cluster via
`tools/run_authority_integration.sh` (or a new recruitment-specific
integration script) before the next deployment.

**Scope:** distributed consensus over a stigmergic (no-coordinator)
recruitment protocol on CockroachDB: same-memory concurrent migration,
authority revocation mid multi-round protocol, and the sweeper's chunked
orphan sweep racing live recruitment.

## Findings

| ID | Vector | Verdict |
|---|---|---|
| R3-01 | Two agents resolving consensus for the same memory concurrently | **Not exploitable.** `resolve_recruitment` locks the memory row `FOR UPDATE` (`ops/recruitment.py`) before deciding, so the second resolver blocks until the first's read-decide-write-audit unit fully commits, then re-reads a settled world. Matches Round 2's R2-05. |
| R3-02 | Target region reshaped/retired concurrently with a resolution reading its status | **Real gap, fixed.** The region-status read had no lock (plain `SELECT`), while every other decision-relevant row in the same function is read `FOR UPDATE`/`FOR SHARE`. Nothing in the current codebase mutates `memory_regions.status` after creation (`ops/regions.py` is create-only), so this was not yet reachable — but it was a live TOCTOU waiting for Phase 2 split/merge. **Fixed**: the read now takes `FOR SHARE` (REC-011 follow-through). |
| R3-03 | Capability revoked mid multi-round protocol (signal emitted in round 1, resolved in round N) | **Real gap, open.** `revoke_node` revokes `agent_nodes`, `node_capabilities`, `node_region_capabilities` but not `recruitment_signals`. A signal recorded under a node's now-revoked `SIGNAL` capability stays `PENDING` and still votes in a later `resolve_recruitment` — the resolver only checks its own `RESOLVE` capability, never the historical emitter's. Whether this is a bug or an intended stigmergy property (a pheromone outlives the ant that laid it; capability is enforced at write time, not continuously re-validated) is a product decision, not something to silently patch — **left open pending direction.** Closing it for real needs a schema change: `recruitment_signals` does not currently record which node emitted a signal, only `origin_region`. |
| R3-04 | Sweeper drains `SWEEP_CHUNK_SIZE` memories per transaction; a stale-but-not-yet-swept memory recruited concurrently | **Not the hypothesized split-brain** — each chunk is its own transaction with its own `now()` (`lambdas/common.py::bounded_drain`), and `sweep_orphans` recomputes staleness from scratch every chunk, so there is no cached "should be ORPHANED" flag to disagree with the live `tier` column; a concurrent write to the same row blocks on `FOR UPDATE` like R3-01. **But a real bug was hiding behind the question**: `resolve_recruitment` never checked the memory's current tier. If the sweeper won the race and set `tier = 'ORPHANED'` first, a subsequent consensus migration silently overwrote it back to `'REGIONAL'` under a plain `MEMORY_MIGRATED` event — resurrecting the memory without the `REDISCOVERED` audit semantics `recall()` deliberately gives the same transition (`ops/memories.py`). **Fixed**: `resolve_recruitment` now raises `MemoryOrphaned` and refuses; `recall()` is the only sanctioned way out of `ORPHANED`. |

## What changed

- `ops/recruitment.py`: target region's `memory_regions.status` read is now
  `SELECT ... FOR SHARE` (was an unlocked `SELECT`).
- `ops/recruitment.py`: new `MemoryOrphaned` exception; `resolve_recruitment`
  raises it before touching `recruitment_signals` when the locked memory row's
  `tier == 'ORPHANED'` (REC-018).
- `ops/__init__.py`: exports `MemoryOrphaned`.
- `lambdas/changefeed_resolver.py`: classifies `MemoryOrphaned` as an expected
  outcome (`"orphaned"` in the batch summary), not an incident — consistent
  with how `CooldownActive`/`RegionUnavailable`/`LookupError` are already
  handled.
- `demo/run_demo.py`: the resolver loop defers on `MemoryOrphaned` the same
  way it defers on `CooldownActive`/`RegionUnavailable`.
- `tests/test_recruitment_pure.py`: added a scripted-cursor test proving the
  `ORPHANED` guard fires before the live-signals query is ever issued.

## Open item

R3-03 (revoked node's already-recorded vote) is documented, not fixed.
Closing it requires deciding whether recruitment votes are a property of the
*region* (current design — no node attribution stored, matches the
stigmergy metaphor) or the *node* (would need a schema column and a
resolution-time re-check, and would raise its own question: does revoking
one node's `SIGNAL` capability for a region invalidate a vote other
still-active nodes could have cast identically?).

## Boundary / remaining work

No real CockroachDB cluster was exercised this round — the two fixes above
are validated by static reasoning and pure unit tests only. Before shipping,
run them against a live cluster the way Round 2 did, ideally adding a
concurrent-resolver and a sweeper-vs-resolver race to
`tools/run_authority_integration.sh` or a sibling script.
