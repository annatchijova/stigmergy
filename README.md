# STIGMERGY

Multiple autonomous agents build and reinforce a shared episodic memory
by interacting **only through distributed state in CockroachDB**. No
agent coordinates another; no LLM sits in any decision path; every state
transition is sealed into per-node hash chains committed to a chained
Merkle ledger. Convergence without a coordinator, verifiable without a
central authority.

Built for the **CockroachDB × AWS — Build with Agentic Memory**
hackathon. Apache 2.0.

**Live:** <https://annatchijova.github.io/stigmergy/> — the explainer page, the
runnable field console (with one-click replay of a real sealed run), and the
coordination and custody viewers.

## Hackathon components (CockroachDB × AWS)

**CockroachDB — two required components:**

- **Distributed Vector Indexing.** `memories.embedding VECTOR(384)` with a
  `VECTOR INDEX (region_id, embedding)` (`schema.sql`), applied to the live
  CockroachDB Cloud cluster; `recall()` runs a region-scoped `<->` vector search
  over the pinned `all-MiniLM-L6-v2` embeddings.
- **ccloud CLI (Agent-Ready).** The `combat-mummy` cluster is inspected and
  managed with `ccloud` — `ccloud cluster list`, `ccloud cluster info`,
  `ccloud cluster user`, `ccloud cluster sql` — the agent-ready surface for
  cluster and SQL-principal management.

**AWS.** The resolver and sweeper are deployed as **AWS Lambda** functions
(`infra/template.json`): a public resolver Function URL gated by the changefeed
token, and an **EventBridge**-scheduled sweeper, each with its own **IAM** role
and **Secrets Manager** DSN, connecting to the CockroachDB Cloud cluster (itself
on AWS, `us-east-1`). Both identity smoke-tests pass (resolver heartbeat 200,
sweeper tick 200). A GCP Cloud Run deployment (`gcp/`) mirrors the same identity
contract on a second substrate.

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

## Using it end to end

Five steps, from nothing installed to a cloud deployment. Each step is real and
independently verifiable — none of them asks you to trust a screenshot. Do them
in order the first time; the mental model builds as you go.

**0 — What you need.** Python 3.11+, the `cockroach` binary (v25.2+, for the
`VECTOR` index), and `psycopg`. For the semantic run also `sentence-transformers`
(it pulls torch). On an externally-managed Python (Debian/Ubuntu) use a venv or
`pip install --break-system-packages`.

**1 — See the decision math and the audit, in a browser (zero setup).**
Open `demo/console.html` — no server, no build, no database. Press **Run the
field**: agents recall, reinforce, and migrate memories by consensus, every step
sealed into a hash chain. Then edit any payload and press **Verify** — the page
names the forgery, with the sealed hash, the recomputed hash, and the sequence
where the chain stops relinking. This is a *simulation of the field with the
exact rational arithmetic and real SHA-256* — not the database, and it says so on
its face.

**2 — Run the real system locally (real CockroachDB, one command).**

    tools/run_secure_demo_local.sh                                  # deterministic provider
    STIGMERGY_DEMO_PROVIDER=minilm tools/run_secure_demo_local.sh   # semantic (the take worth recording)

Starts a disposable local cluster, applies `schema.sql`, creates **one
authenticated SQL principal per node**, bootstraps authority, runs the field,
verifies every hash chain and the Merkle ledger, **exports a sealed evidence
bundle**, verifies it with an independent verifier, then deletes the cluster.
The bundle outlives the cluster — that is the point.

**3 — Re-examine that real run with no cluster at all.**
Open `demo/console.html` → **Replay the real run** (a real sealed bundle ships
with the page), or **Open a sealed bundle** and pick your own `run.bundle.json`.
The page renders the real field and re-runs the same six checks (B1–B6) in the
browser — a second, independent implementation of the verifier. Edit one
`region_id` in the file and B5 fails: you can edit the row, you cannot edit the
evidence.

**4 — Run against a cluster you own.**
Apply the schema, then register the node identities and grants
(`AUTHORITY_MODEL.md`; `tools/bootstrap_prod_authority.py` finishes the audited
bootstrap) and create one SQL user per node
(`tools/provision_service_users.sh`). Then:

    python -m demo.run_demo --dsn "$SEED_DSN" --seed-dsn "$SEED_DSN" \
        --agent-dsn "$AGENT_0" --agent-dsn "$AGENT_1" --agent-dsn "$AGENT_2" \
        --resolver-dsn "$RESOLVER_DSN" \
        --agents 3 --rounds 20 --provider minilm --local-resolver --bundle run.bundle.json

One authenticated principal per node is required by design — a shared connection
cannot masquerade as many nodes.

**5 — Deploy the always-on resolver and sweeper to the cloud.**
Google Cloud Run: `bash gcp/deploy.sh` ([gcp/README.md](gcp/README.md)).
AWS Lambda: `infra/template.json` ([infra/README.md](infra/README.md)). Both keep
one node = one CockroachDB principal = one secret; the resolver is public but
gated by a changefeed token, the sweeper is private and scheduler-invoked. See
[Deploying to Google Cloud](#deploying-to-google-cloud-cloud-run) below.

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
[TODO.md](TODO.md). How to record the demo — and what the recording must not
imply — is in [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md).

## Layout

    audit/        canonical JSON + quantization, per-node hash chains,
                  chained Merkle ledger, sealed evidence bundles
                  (canonical.py, chain.py, merkle.py, bundle.py)
    embeddings/   provider protocol; deterministic dev provider
                  (is_semantic=False) and MiniLM (384-dim, semantic)
    ops/          memories (store/recall/reinforce), recruitment
                  (signals, exact-Fraction consensus, cooldown
                  migration), orphans (bounded sweep), controller
                  (roaming/dwelling hysteresis), regions (audited creation)
    lambdas/      changefeed webhook resolver + cron sweeper
    gcp/          Cloud Run adapter (main.py) + one-command deploy (deploy.sh)
    demo/         corpus with deliberately misplaced memories + harness,
                  console.html (runnable field + bundle verifier, no install)
    tools/        one-command local secure demo, bundle verifier, embedding
                  bake step, authority regression runner
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

230 assertions: canonicalization, hash sensitivity, Merkle ambiguity,
reinforcement closed forms, consensus at exact boundaries, hysteresis at
exact thresholds, envelope parsing, drain budgets, lineage discipline,
and the build-time contract between the browser console and the vector
table baked for it.
Transactional behavior is deliberately NOT faked with mock cursors — it
runs against the real cluster per `tests/INTEGRATION_CHECKLIST.md`.

To run the authority regression on a disposable local CockroachDB node:

    ./tools/run_authority_integration.sh

It starts a localhost-only temporary cluster, applies the schema, proves that
principal impersonation, cross-agent controller mutation, and revoked writes
are rejected, verifies the chain, then removes the cluster data.

## Running the demo

One command, if you have the `cockroach` binary (v25.2+) and `psycopg`:

    tools/run_secure_demo_local.sh

It starts a disposable localhost cluster, applies the schema, creates one
authenticated principal per node, performs the trusted bootstrap, runs the
field, verifies every chain and the ledger, **exports a sealed evidence
bundle, verifies it with the independent verifier**, and deletes the
cluster. The bundle outlives the cluster, which is the point. It
auto-detects and announces its provider; without `sentence-transformers`
it runs `deterministic` and tells you what that costs you.

For the recording itself — shot order, measured timings, and the
sentences that carry each beat — see [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md).

The manual path, for a cluster you already have. Requires CockroachDB
v25.2+ with `SET CLUSTER SETTING feature.vector_index.enabled = true;`
and the schema applied (`cockroach sql < schema.sql`).

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

It also loads **sealed evidence bundles** exported from a real cluster run (see
below), which is how it stops being a simulation and becomes a dashboard over
actual runs.

What it is not, when it is simulating: a CockroachDB deployment, a changefeed,
or a Lambda. There is
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
  which the page loads on reload and relabels itself accordingly. The table
  is a build artifact, absent from a fresh clone by design.

  A table that does **not** cover the console's corpus is refused, with the
  count of missing texts stated in the page's own provider note. This is not
  defensive decoration: a missing vector scores distance 1, so a partial
  table would collapse recall to insertion order while the page claimed the
  pinned semantic model — `is_semantic`'s failure mode arriving through a
  build step instead of a provider. Full coverage or the labeled fallback,
  nothing in between, and `tests/test_console_contract_pure.py` pins the
  seam so the two halves cannot drift apart in silence again.

## Sealed evidence bundles

A bundle is one JSON file holding everything needed to re-examine a run without
the cluster that produced it: the state tables as they stood, every per-node
hash chain, and every Merkle snapshot, sealed with a hash over its own canonical
form. It turns the console from a simulation into a **dashboard over real runs**.

    python -m demo.run_demo --dsn ... --bundle run.bundle.json   # export
    python -m tools.verify_bundle run.bundle.json                # verify

Then open `demo/console.html`, press **Open a sealed bundle**, and the page
renders the real field and runs the same six checks in JavaScript. Served over
HTTP it also accepts `?bundle=<url>`.

Six checks, in both implementations:

| | claim |
|---|---|
| B1 | the bundle as shipped is the bundle as sealed |
| B2 | every chain relinks: genesis, dense sequence, `prev_hash` linkage |
| B3 | every entry hash recomputes from its stored payload |
| B4 | the content shipped is the content born |
| B5 | declared memory state reproduces from replaying the chains |
| B6 | every Merkle snapshot recomputes over its recorded heads |

B5 is the one that matters most: a state column edited to something the chain
never authorized stops replaying. You can edit the row; you cannot edit the
evidence.

**Two independent verifiers, and that is the point.** `audit/bundle.py` and the
JavaScript in `demo/console.html` implement the same six checks separately. If
they ever disagree about a bundle, one of them is wrong.
`tests/fixtures/cross_impl.bundle.json` is sealed by the Python canonicalizer
over deliberately awkward content — accents, CJK, a quote, a backslash, a tab, a
newline, an astral-plane emoji — and B1 passing in the browser is what proves
the two canonicalizers agree byte for byte. The console can also export its own
simulated run as a bundle, which `tools/verify_bundle.py` then verifies; that
round trip is exercised and passes.

**What a bundle does not prove**, stated because a verifier that implies more
than it checks is worse than none:

1. **That the writer was authorized.** Authority is enforced at the database
   (`AUTHORITY_MODEL.md`) and leaves no artifact a detached file can re-check.
   A bundle shows what happened, not that it was allowed.
2. **That nothing was omitted wholesale.** Dropping a node's chain entirely
   leaves an internally consistent bundle. B6 catches it only when a previously
   published Merkle snapshot already committed to that node — so publish your
   roots.
3. **Anything about the per-memory custody chains** (`audit/custody.py`). Those
   have their own verifier and are out of scope for bundle version 1.

## Deploying to Google Cloud (Cloud Run)

`gcp/deploy.sh` is the Google Cloud counterpart to the AWS SAM scaffold below —
same identity contract, different substrate. It provisions three Secret Manager
secrets, a per-service runtime service account, two Cloud Run services, and the
Cloud Scheduler tick, then prints the service URLs and the identity smoke-tests.
Full order and residual boundaries: [gcp/README.md](gcp/README.md).

    export GCP_PROJECT=your-project-id
    export RESOLVER_DSN='postgresql://stigmergy_resolver:...@host:26257/stigmergy?sslmode=verify-full'
    export SWEEPER_DSN='postgresql://stigmergy_sweeper:...@host:26257/stigmergy?sslmode=verify-full'
    export CHANGEFEED_TOKEN="$(openssl rand -hex 32)"
    bash gcp/deploy.sh

- **resolver** — Cloud Run public at the IAM layer (CockroachDB's webhook cannot
  present a GCP OIDC token) but gated by the constant-time
  `x-stigmergy-changefeed-token` check inside the handler.
- **sweeper** — Cloud Run private; Cloud Scheduler invokes it with OIDC and
  `roles/run.invoker`.

`gcp/main.py` is a thin HTTP wrapper over the same handlers the Lambdas use — the
domain core is untouched, and the deployment secret arrives as a plain env var,
so no cloud SDK is added. CockroachDB Cloud signs with a private CA, so the image
bakes the cluster root at libpq's default path (`sslmode=verify-full` needs no
`sslrootcert` in the DSN).

## Deploying the Lambdas (AWS)

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
