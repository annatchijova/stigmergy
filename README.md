# STIGMERGY

Multiple autonomous agents build and reinforce a shared episodic memory
by interacting **only through distributed state in CockroachDB**. No
agent coordinates another; no LLM sits in any decision path; every state
transition is sealed into per-node hash chains committed to a chained
Merkle ledger. Convergence without a coordinator, verifiable without a
central authority.

Built for the **CockroachDB × AWS — Build with Agentic Memory**
hackathon. Apache 2.0.

## Why this exists

Most multi-agent systems coordinate through orchestration: a controller
process, a message bus, or an LLM deciding who does what next. STIGMERGY
coordinates through memory instead. Agents never talk to each other —
each one only reads and writes shared state in CockroachDB, the way ants
coordinate through pheromone trails left in a shared environment (the
biological phenomenon the project is named after).

An agent notices a memory sitting away from where it belongs and writes
a recruitment signal. Other agents read that signal and vote with exact
`Fraction` weights; once consensus crosses a threshold, the memory
migrates home under a cooldown. No agent ever instructs another agent to
do anything — the database is the only channel, and every step that
changes shared state is sealed into a verifiable audit trail as it
happens.

```
   Agent A        Agent B        Agent C
      │               │               │
      └───────┬───────┴───────┬───────┘
              │               │
              ▼               ▼
      ┌──────────────────────────┐
      │       CockroachDB        │  <- the only channel between agents
      │  memories · recruitment  │
      │  signals · hash chains   │
      └──────────────────────────┘
             │
             ▼
  recruitment signal observed
             │
             ▼
  exact-Fraction weighted consensus
             │
             ▼
  migration under cooldown
             │
             ▼
  per-node hash chain → Merkle ledger
```

No orchestrator, no message queue, no agent-to-agent call — every arrow
above is a read or a write against CockroachDB. Design document:
`ARCHITECTURE.md`. Schema (the real spec — most invariants are
constraints, not conventions): `schema.sql`.

## Authority after the MNEME red-team transfer

Hash chains show what happened; they do not authorize the writer. STIGMERGY
binds every mutable `node_id` to CockroachDB's authenticated database principal
and requires an explicit capability for the relevant region. A revoked node
cannot keep storing, reinforcing, signalling, resolving, or running
maintenance merely by naming its old node id.

Read [AUTHORITY_MODEL.md](AUTHORITY_MODEL.md) before deploying mutable agents:
it covers trusted bootstrap, least-privilege principals per Lambda/agent, the
capability vocabulary, and the remaining trust boundary.
For the concrete AWS Lambda ↔ CockroachDB service-account mapping, read
[docs/AWS_COCKROACH_DEPLOYMENT_CONTRACT.md](docs/AWS_COCKROACH_DEPLOYMENT_CONTRACT.md).
The account-free, reviewable AWS SAM scaffold lives in
[infra/README.md](infra/README.md).

The executed adversarial evidence is recorded in
[docs/SECURITY_AUDIT_ROUND_2.md](docs/SECURITY_AUDIT_ROUND_2.md).
The implementation and reproducibility work is summarized in
[docs/ENGINEERING_LOG_2026-07-21.md](docs/ENGINEERING_LOG_2026-07-21.md).
The honest Cloud-deployment, demo, and post-submission sequence is tracked in
[TODO.md](TODO.md).

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

To run the authority regression on a disposable local CockroachDB node:

    ./tools/run_authority_integration.sh

It starts a localhost-only temporary cluster, applies the schema, proves that
principal impersonation, cross-agent controller mutation, and revoked writes
are rejected, verifies the chain, then removes the cluster data.

## Running the demo

Requires CockroachDB v25.2+ with `SET CLUSTER SETTING
feature.vector_index.enabled = true;` and the schema applied
(`cockroach sql < schema.sql`).

    pip install psycopg sentence-transformers
    python -m demo.run_demo --dsn "$STIGMERGY_REPORT_DSN" \
        --seed-dsn "$STIGMERGY_SEED_DSN" \
        --agent-dsn "$STIGMERGY_AGENT_0_DSN" \
        --agent-dsn "$STIGMERGY_AGENT_1_DSN" \
        --agent-dsn "$STIGMERGY_AGENT_2_DSN" \
        --resolver-dsn "$STIGMERGY_RESOLVER_DSN" \
        --agents 3 --rounds 20 --provider minilm --local-resolver

The secure demo uses one authenticated CockroachDB principal per node: seeder,
each agent, and resolver. Register the matching node ids and grants first as
described in `AUTHORITY_MODEL.md`; `--agent-dsn` is intentionally required once
per agent so a single shared connection cannot masquerade as many nodes.
For the local video setup, see `tools/secure_demo_local.md`.

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

## Running the field console (no database, no install)

`demo/console.html` opens directly in a browser — no server, no build, no
dependencies — and runs the field live: three agents recall, reinforce, emit
recruitment signals, and migrate memories by consensus, with nothing passing
between them but shared state.

What it reproduces exactly, not approximately:

- **The decision arithmetic.** Reinforcement `c' = c + (1−c)·1/5`, hysteresis
  EMA with `β=1/4` and enter `3/5` / exit `2/5`, consensus
  `vigor_for ≥ 1/2 · live_signals` — all in BigInt rationals. No float reaches
  a verdict; the one float is the read-time decay that gates liveness, exactly
  as in `ops/recruitment.py`.
- **The audit construction.** Canonical JSON (sorted keys, no whitespace,
  decimals as fixed-point strings at scale 10), `entry_hash =
  sha256(prev_hash ‖ envelope)`, genesis `sha256("STIGMERGY_GENESIS")`, Merkle
  leaves `sha256(node_id ":" head)`. SHA-256 is implemented in the page (so it
  works from `file://`) and matches the standard test vectors. **Rewrite any
  payload in the page and verification names the forgery**, giving the sealed
  and recomputed hashes and the sequence number where the chain stops relinking.

What it is not: a CockroachDB deployment, a changefeed, or a Lambda. There is
no database, no authority model checking principals, and the resolver is an
in-page loop — the same labeled stand-in `--local-resolver` is. The migration
cooldown is compressed from five minutes to eight seconds. The page says all of
this on its face.

Two honest deviations, both stated in the page:

- The console seeds **its own corpus**, not `demo/corpus.py`. The cluster corpus
  is written for the pinned MiniLM provider, and under a non-semantic provider
  `run_demo.py` refuses to narrate convergence at all. Rather than fake a
  semantic claim, the console uses a corpus where lexical distance is a truthful
  signal about the three regions.
- Recall therefore runs a tf-idf cosine, not MiniLM. To swap in the real
  vectors, run `python -m tools.bake_embeddings` (needs
  `pip install sentence-transformers`); it writes `demo/minilm_vectors.js`,
  which the page picks up automatically and relabels itself accordingly.

## Deploying the Lambdas

The repository includes `infra/template.json`, which provisions separately
scoped Lambda roles, Secrets Manager access, a resolver Function URL, and the
EventBridge sweeper schedule. It has static contract coverage without an AWS
account; its deployment sequence and explicit non-claims are in
[`infra/README.md`](infra/README.md).

Both require env: `STIGMERGY_NODE_ID` (a Lambda IS a node; identity is
deployment configuration) and `STIGMERGY_DSN`. On every valid invocation they
prove that the authenticated CockroachDB principal owns that node; the sweeper
also proves `MAINTAIN` before it can report an empty successful sweep. A
mispointed secret therefore fails closed instead of looking healthy.

The public resolver additionally requires `STIGMERGY_CHANGEFEED_TOKEN`. Put it
in the resolver's separate secret and configure the same value as CockroachDB
changefeed `extra_headers`; requests without the exact header receive `401`
before parsing or opening a database connection.

**Changefeed resolver** (`lambdas/changefeed_resolver.py:handler`) —
point a webhook-sink changefeed at its function URL:

    CREATE CHANGEFEED FOR TABLE recruitment_signals
      INTO 'webhook-https://<function-url>'
      WITH updated, resolved = '30s',
           extra_headers = '{"x-stigmergy-changefeed-token":"<secret>"}';

At-least-once delivery is safe: resolution is idempotent through the
state machine. Expected outcomes ACK; unexpected failures fail the Lambda
invocation (the sink redelivers and CloudWatch counts an error); malformed
entries ACK with a named report so a poison
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
