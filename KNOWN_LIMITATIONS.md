# STIGMERGY — Known limitations

This file exists because `schema.sql` and `ARCHITECTURE.md` promise it
by name. Every entry here is documented at its source too; this is the
consolidated ledger. The organizing principle is the Failure philosophy:
a limitation named is a decision; a limitation hidden is a bug waiting
for a better moment.

## Pinned embedding model (referenced from the schema header)

`VECTOR(384)` is bound to exactly ONE model at a time. The dimension's
single source of truth is `embeddings.base.EMBEDDING_DIM = 384`; the
providers are:

- `all-MiniLM-L6-v2` (`embeddings/minilm.py`) — semantic, used for the demo.
- `deterministic-sha256-v1` (`embeddings/deterministic.py`) — NON-semantic
  (`is_semantic = False`), development plumbing only. No relevance or
  convergence claim may be derived from its distances, and code that
  tries is violating a declared contract, not discovering a bug.

Mixing vectors from different models in one column is semantically
meaningless even when dimensions coincide. `verify_provider()` refuses a
database populated by a different provider (the audit chain's
`MEMORY_STORED.provider_id` is the source of truth). **Changing models
is a migration event, not a config flip.** The startup cross-check
between `EMBEDDING_DIM` and the declared column dimension is on
`tests/INTEGRATION_CHECKLIST.md`, not yet implemented.

## Accepted floating-point exceptions

Exactly two, both outside every decision path:

1. **Recruitment decay** — `signal_strength * exp(-decay_rate * Δt)`,
   computed in SQL at READ time over immutable `created_at`, cast
   `FLOAT8` end to end. It gates liveness; it never votes. Votes are
   exact `Fraction` vigor; the verdict is a `Fraction` comparison.
2. **Retry backoff jitter** (`run_in_transaction`) — wall-clock
   scheduling for `time.sleep()`, deliberately random. Nothing is
   decided, hashed, or verified over these values.

## Trust model (out of scope, documented, not defended against)

- Byzantine consensus, real-time guarantees, and distributed transactions
  across independent deployments remain out of scope. Node/region authority
  is enforced for callers using the reviewed runtime and distinct authenticated
  CockroachDB principals; a stolen DB credential or database superuser remains
  outside that application-level guarantee.

Consensus dilution (REC-002: foreign live signals enlarge the
denominator) raises the cost of cherry-picking a target, but it is a
robustness property, not a Sybil defense.

The bar is lower than "multiple regions", and honesty demands saying so
(REDTEAM F-2). Consensus counts each live signal as one voter, so absent
a per-region cap a SINGLE region could fabricate consensus by emitting
many signals for one memory. `emit_signal` now requires the authenticated node
to hold an active `SIGNAL` capability for `origin_region`; the one-live-signal
index remains the independent anti-flood guard. This is not Byzantine
consensus: distinct compromised credentials can still represent distinct
legitimate regions.

**A revoked node's already-emitted vote is not retracted (Round 3, R3-03).**
`recruitment_signals` records `origin_region`, not the emitting node —
capability is enforced at write time (`emit_signal` requires an active
`SIGNAL` grant), never re-checked at resolution time. `revoke_node`
revokes `agent_nodes`/`node_capabilities`/`node_region_capabilities` but
does not touch `recruitment_signals`: a `PENDING` signal emitted before
revocation keeps voting in any later `resolve_recruitment` call until it
is accepted or expires on its own TTL. This is consistent with the
stigmergy metaphor (a pheromone outlives the ant that laid it) and with
the audit chain's own stance (it records who acted, not who is
*currently* authorized) — but it is a real design choice, not yet a
reviewed one, and closing it the other way would need a schema column
attributing each signal to its emitting node.

## Deferred to Phase 2

- **Region split/merge** — requires explicit CAS serialization on
  `memory_regions.status`. `resolve_recruitment` already reads that
  status `FOR SHARE` (Round 3 red team, REC-011 follow-through) so a
  concurrent status change cannot slip between the check and the
  migration `UPDATE`; the reshape operation itself — the thing that
  would actually change `status` after creation — still does not exist
  (`ops/regions.py` is create-only today).
- **GLOBAL-tier policy** — GLOBAL memories do not orphan (excluded by
  design from the sweep) and a recruitment migration demotes them to
  REGIONAL (visible in the `MEMORY_MIGRATED` payload as
  `old_tier`/`new_tier`). Consolidation — how a memory earns and keeps
  GLOBAL — is future work; today nothing promotes to GLOBAL.
- **Confidence decay** — REINFORCED is currently permanent unless
  re-reinforced past saturation (stored confidence saturates at exactly
  1 after step 104 from c₀ = 1/2; documented and pinned by test). A
  decay module moving stale REINFORCED → NEUTRAL would feed the orphan
  sweep without ever letting it touch reinforced memories directly.

## Operational costs, accepted and bounded

- **Orphan-sweep staleness scan** — the `greatest(...)` expression has
  no covering index; each sweep chunk scans the non-orphaned working
  set. Accepted as a low-frequency cron cost rather than half-fixed
  with a wrong index (`ops/orphans.py`).
- **Audit chains grow forever** — nothing is pruned, in the spirit of
  Invariant 8. Merkle snapshots chain linearly. There is no archival or
  compaction story yet; at hackathon scale this is storage, not risk.
- **Sweep events are bounded, not small** — `SIGNALS_EXPIRED` and
  `MEMORIES_ORPHANED` payloads carry up to `limit` ids per event; the
  cron loops chunks (`bounded_drain`) and reports `drained: false` when
  budget runs out before backlog does.

## Custody layer (ported from MNEME — audit/custody.py, ops/trust.py)

- **Direct taint only.** A memory is flagged if a node's own custody
  chain names it as `actor_id` (except `CONTRADICTED_BY` — see
  `ops/trust.py`'s module docstring for why). TRANSITIVE taint — a
  flagged memory's RESONANT neighbours inflated by association — is
  real but unbounded; `quarantine_actor` reports the one-hop RESONANT
  neighbourhood as an ADVISORY list, never auto-flags it. Automatic
  transitive flagging without a fixpoint bound is how a quarantine
  becomes a self-inflicted denial of service on the field.
- **No supersession.** `SUPERSEDED` custody status and `SUPERSEDED_BY`
  event type are deliberately not built — no `supersede()` flow exists
  in `ops/memories.py` yet. If one is added later, the custody
  vocabulary needs a matching, reviewed extension (it is closed on
  purpose — see `audit/custody.py`).
- **No per-actor un-sweep.** `taint_sweeps` has `UNIQUE(quarantined_actor)`
  — a node can be swept once. Only `rehabilitate_memory` exists
  (per-memory, TAINT_FLAGGED → CLEAN, audited). Reversing an entire
  sweep at once has no designed review path in Phase 1, same limitation
  MNEME itself documents.
- **`REGION_ADMIN` is global, not per-region**, despite gating
  `quarantine_memory`/`rehabilitate_memory` as if it were regional
  authority — an existing quirk of `ops/authority.py`'s capability
  model that this port inherits and surfaces (see `AUTHORITY_MODEL.md`)
  rather than working around with new region-scoped semantics.
- **`demo/field_viewer.html` is a staged, hand-authored replay**, not a
  live capture of a real cluster run — same honesty note MNEME's own
  README carries for the original. It visualizes the custody+taint
  mechanics that `audit/custody.py`/`ops/trust.py` actually implement;
  it does not call them. The verifier of record is
  `audit.custody.verify_custody_chain` against live rows, not the
  viewer's display hashes (`pseudoHash`, explicitly cosmetic).
- **No sealed evidence bundle export yet.** MNEME's `bundle.py` /
  `verify_offline.py` (export the whole field as one hash-sealed file, a
  distrusting third party verifies offline with one stdlib script) was
  not ported in this pass — natural fast-follow once custody+taint have
  Cloud integration-checklist evidence (`tests/INTEGRATION_CHECKLIST.md`).
  `demo/field_viewer.html`'s "EXPORT EVIDENCE BUNDLE" beat is labeled
  `(PLANNED)` for exactly this reason.

## Demo-only substitutes

- `--local-resolver` POLLS pending signals as a stand-in for the
  changefeed Lambda. The deployed system reacts
  (`lambdas/changefeed_resolver.py`); the flag exists so the demo runs
  on a laptop without AWS, and it is labeled as the substitute it is.
- Lambda node identity is deployment configuration
  (`STIGMERGY_NODE_ID` required): sandboxes are ephemeral, and
  auto-derived per-sandbox identities would flood the ledger with
  short-lived chains.
