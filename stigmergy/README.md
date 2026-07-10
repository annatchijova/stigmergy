# STIGMERGY

Multiple autonomous agents build and reinforce a shared episodic memory
by interacting **only through distributed state in CockroachDB**. No
agent coordinates another; no LLM sits in any decision path; every state
transition is sealed into per-node hash chains committed to a chained
Merkle ledger. Convergence without a coordinator, verifiable without a
central authority.

Built for the **CockroachDB × AWS — Build with Agentic Memory**
hackathon. Apache 2.0. Design document: `ARCHITECTURE.md`. Schema (the
real spec — most invariants are constraints, not conventions):
`schema.sql`.

## Layout

    audit/        canonical JSON + quantization, per-node hash chains,
                  chained Merkle ledger  (canonical.py, chain.py, merkle.py)
    embeddings/   provider protocol; deterministic dev provider
                  (is_semantic=False) and MiniLM (384-dim, semantic)
    ops/          memories (store/recall/reinforce), recruitment
                  (signals, exact-Fraction consensus, cooldown
                  migration), orphans (bounded sweep), controller
                  (roaming/dwelling hysteresis), regions (audited creation)
    lambdas/      changefeed webhook resolver + cron sweeper
    demo/         corpus with deliberately misplaced memories + harness
    tests/        pure suites (no DB) + INTEGRATION_CHECKLIST.md

## The disciplines, in one paragraph

Floats never decide: decisions are exact `Fraction` arithmetic,
quantized to `DECIMAL(11,10)` only at the SQL/audit boundary (the one
sanctioned `exp()`-over-floats is signal decay, computed at READ time
and confined to a liveness gate). Every module takes a live cursor and
never commits — state change and audit event share one transaction, so
Invariant 3 ("no unaudited transition") is true by construction. Nothing
is ever deleted: `FORGOTTEN` and `ORPHANED` are states; rediscovery is
an audited event. Boundaries reject with our words, not the driver's
(FK/PK violations are translated where they can occur).

## Running the pure tests (no database)

    cd stigmergy
    for t in tests/test_*_pure.py; do python3 "$t"; done

~160 tests: canonicalization, hash sensitivity, Merkle ambiguity,
reinforcement closed forms, consensus at exact boundaries, hysteresis at
exact thresholds, envelope parsing, drain budgets, lineage discipline.
Transactional behavior is deliberately NOT faked with mock cursors — it
runs against the real cluster per `tests/INTEGRATION_CHECKLIST.md`.

## Running the demo

Requires CockroachDB v25.2+ with `SET CLUSTER SETTING
feature.vector_index.enabled = true;` and the schema applied
(`cockroach sql < schema.sql`).

    pip install psycopg sentence-transformers
    python -m demo.run_demo --dsn "$STIGMERGY_DSN" \
        --agents 3 --rounds 20 --provider minilm --local-resolver

The corpus seeds three themed regions plus six **deliberately misplaced
memories**. Agents (each a distinct audit node) recall, reinforce, and
emit recruitment signals when the density gradient says a memory sits
away from its resonant neighborhood; weighted exact-Fraction consensus
migrates it home under the Invariant-6 cooldown. The report prints the
misplaced-memories scoreboard, verifies every node's chain, takes a
Merkle snapshot, and verifies the ledger end to end.

`--provider deterministic` runs without torch and exercises every
mechanism, but the report will refuse to narrate convergence:
`is_semantic=False` means distance-based claims would be fabricated
certainty, and the demo obeys its own Failure philosophy.

`--local-resolver` is a labeled, demo-only polling stand-in for the
changefeed Lambda so the demo runs on a laptop without AWS.

## Deploying the Lambdas

Both require env: `STIGMERGY_NODE_ID` (a Lambda IS a node; identity is
deployment configuration) and `STIGMERGY_DSN`.

**Changefeed resolver** (`lambdas/changefeed_resolver.py:handler`) —
point a webhook-sink changefeed at its function URL:

    CREATE CHANGEFEED FOR TABLE recruitment_signals
      INTO 'webhook-https://<function-url>'
      WITH updated, resolved = '30s';

At-least-once delivery is safe: resolution is idempotent through the
state machine. Expected outcomes ACK; unexpected failures 500 (the sink
redelivers); malformed entries ACK with a named report so a poison
message cannot stall the feed.

**Cron sweeper** (`lambdas/cron_sweeper.py:handler`) — EventBridge
schedule. Drains expired signals and stale memories in bounded chunks
(`SWEEP_CHUNK_SIZE` × `SWEEP_MAX_CHUNKS` per tick, `ORPHAN_WINDOW_SECONDS`
staleness); reports `drained: false` when work remains for the next tick.

## Known limitations

Consolidated in `KNOWN_LIMITATIONS.md` (the file `schema.sql` and
`ARCHITECTURE.md` promise by name): the pinned embedding model and why
changing it is a migration event; the two sanctioned float exceptions;
the trust model (Sybil out of scope); Phase-2 deferrals (split/merge
behind CAS, GLOBAL-tier policy, confidence decay); and the accepted,
bounded operational costs (unindexed orphan-sweep scan, ever-growing
audit chains, chunked sweep events).
