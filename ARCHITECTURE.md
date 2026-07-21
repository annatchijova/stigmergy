# STIGMERGY — Architecture

**Status**: Design frozen for Phase 1 implementation.
**Hackathon**: CockroachDB × AWS — Build with Agentic Memory (Devpost)
**License**: Apache 2.0

## Thesis

Multiple autonomous agents build and reinforce a shared episodic memory by
interacting only through a distributed state in CockroachDB. No agent
coordinates another, yet the system converges on collective memory while
every state transition remains cryptographically verifiable without relying
on a central authority.

This is not a distributed cache in front of a vector store. CockroachDB's
distributed vector index, changefeeds, and multi-region locality are
load-bearing parts of the algorithm, not swappable infrastructure.

## Non-negotiable invariants

These rules exist to survive contact with future optimizations. Any change
that violates one of these requires an explicit architecture decision, not
a quiet patch.

1. **Shared state decides. No agent holds private truth.** Every agent
   action is a reaction to what it currently reads from CockroachDB — never
   to local cache, in-memory history, or another agent's direct message.
   Agents communicate exclusively by writing and observing shared state
   (stigmergic coordination).

2. **LLMs never sit in the decision path.** An LLM may label a region's
   dominant topic for a dashboard, or narrate why a decision chain
   occurred. An LLM may never decide whether a memory reinforces, migrates,
   consolidates, splits, or merges a region. All such decisions are
   deterministic functions over `Fraction`/`DECIMAL` arithmetic and SQL
   aggregates. This mirrors the same invariant enforced in VIGÍA.

3. **No committed state transition may exist without a corresponding audit
   event.** Not "should be audited" — cannot exist unaudited. Every write to
   `memories`, `cell_links`, `recruitment_signals`, or `memory_regions` that
   changes tier, state, or region must land in the same logical operation
   as its corresponding event in a node's local `audit_chain`, tagged with
   an explicit `event_type` and `reason`. If the audit write fails, the
   state transition is rejected — see Failure philosophy.

4. **Regions are living entities, not fixed partitions.** `memory_regions`
   carry `generation` and `parent_region` so the full lineage of splits,
   merges, and migrations can be reconstructed after the fact. No region is
   permanently authoritative over a fixed set of memories.

5. **No central coordinator for audit ordering. Global ordering is
   intentionally undefined.** This is a property, not a limitation. Each
   node maintains its own local hash chain. Integrity across nodes is
   established via an incremental, chained Merkle ledger
   (`merkle_snapshots`), never via a single global sequence number.

6. **A memory never migrates twice within its cooldown window.** Migration
   decisions are driven by weighted recruitment consensus, not by a single
   acceptance. This exists specifically to prevent oscillation
   (`A → C → A → C`) under fluctuating signal noise.

7. **Recruitment signals decay. They are never permanently binding.**
   `signal_strength` decays exponentially from `created_at`. A late-arriving
   agent can still observe a weak signal, but its influence diminishes with
   time — this is deliberate: pheromones evaporate.

8. **Memories are never deleted.** A memory that stops being recruited
   moves to `ORPHANED`, not to deletion. Rediscovery is always possible and
   is itself an audited event (`REDISCOVERED`).

## System components

| Component | Role |
|---|---|
| `memory_regions` | Logical memory regions, prefix-partitioned in the vector index. Carry lineage (`generation`, `parent_region`) and, in Phase 2, a regional signature (centroid, variance, entropy). |
| `memories` | Episodic memory store. `state` (REINFORCED/NEUTRAL/FORGOTTEN) and `tier` (SHORT_TERM/REGIONAL/GLOBAL/ORPHANED) are independent axes. |
| `cell_links` | Association strength between memories (RESONANT/INHIBITORY), scored via `resonance_weight`. |
| `recruitment_signals` | The stigmergic recruitment protocol. Write-only state, read by other agents/regions — never a message bus. Decays over time via `signal_strength` + `decay_rate`. |
| `agent_search_state` | Per-agent roaming/dwelling controller state, driven by hysteresis over resonance density read from `memories` — not by agent-local confidence. |
| `audit_chain` | Per-node, tamper-evident hash chain of every state-changing event. |
| `merkle_snapshots` | Chained Merkle roots over all nodes' chain heads. A ledger of ledgers. |

## Why CockroachDB is load-bearing, not incidental

- **Vector index prefix columns** (`region_id`) make dwelling (region-constrained
  search) cheap and roaming (cross-region search) expensive by construction.
  The biological metaphor and the query planner's real cost model coincide —
  this is not a narrative layered on top of arbitrary code.
- **Changefeeds**, not polling, drive the recruitment protocol's reactive
  layer (CockroachDB → webhook → AWS Lambda → write-back). CockroachDB is
  used as a consensus/state substrate, never as a message bus.
- **Multi-region locality** (`REGIONAL BY ROW`, eventually) gives physical
  meaning to "region" beyond a mere foreign key — physical nodes and logical
  memory regions are distinct concepts, and the schema does not conflate
  them (per audit correction — regions are logical, nodes are physical).

## Known limitations (to be expanded in KNOWN_LIMITATIONS.md)

- Regional signals are bound to authenticated node principals and explicit
  regional capabilities. Byzantine consensus remains out of scope: multiple
  independently compromised credentials can still represent multiple legitimate
  regions. See `AUTHORITY_MODEL.md`.
- `recruitment_signals` decay uses `exp()` over floats — a deliberate,
  documented departure from the zero-float decision-path discipline used in
  VIGÍA. Justified because this is a biological heuristic affecting
  reinforcement dynamics, not a Daubert-admissible forensic verdict.
- Split/merge of regions is a stretch goal, not part of the Phase 1 core.
  If it ships, it requires explicit CAS-based serialization per region
  (`status` field) to avoid concurrent reshaping of the same region.

## Failure philosophy

STIGMERGY prefers explicit degradation over hidden recovery.

If consensus cannot be established, the system preserves uncertainty.

If migration cannot be completed, the memory remains in place.

If audit cannot be written, the state transition is rejected.

The system never fabricates certainty in order to preserve availability.

## Out of scope

- Byzantine consensus.
- Identity attestation between regions.
- Adversarial node authentication.
- Real-time guarantees.
- Distributed transactions across independent deployments.

## Build environment

- CockroachDB v26.2.2 (self-hosted, local 3-node cluster for development;
  CockroachDB Cloud for the submitted demo).
- Python 3.10+.
- AWS Lambda (two distinct roles: event-driven via changefeed webhook for
  recruitment resolution, and cron-driven for orphan sweeps — deliberately
  not the same pattern reused twice).
