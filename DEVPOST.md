# STIGMERGY

**Coordination without a coordinator. Verifiable without a central authority.**

Submission text for CockroachDB × AWS — Build with Agentic Memory.
Everything below is either demonstrable in a browser tab or explicitly labeled
as not yet executed. That distinction is the project.

---

## The thing that started it

Ants have no manager.

No ant knows the plan. No ant tells another ant what to do. An ant walks, finds
food, and leaves a chemical trace on the way home. Another ant crosses that
trace and is slightly more likely to follow it. Thousands of ants, each running
a stupid local rule, and what emerges is a path — not just any path, close to the
shortest one, rerouted automatically when you put a rock in the way.

The biologists have a word for it: **stigmergy**. Coordination through traces
left in a shared environment. The environment *is* the message.

Now look at how we build multi-agent systems. There is a controller process. Or
a message bus. Or — increasingly — a language model in the middle, deciding who
does what next, in prose, non-deterministically, unaccountably.

We gave the agents a database instead, and took everything else away.

## What it is

Autonomous agents build and maintain a shared episodic memory. They coordinate
**only** through distributed state in CockroachDB. No agent calls another agent.
There is no message queue, no orchestrator, and no LLM anywhere in any decision
path.

An agent recalls memories near its current position in vector space. It notices
that a particular memory is sitting in a neighbourhood where it does not
resonate — the density gradient says so — and it writes a **recruitment signal**:
a trace, in a table, that decays over time exactly like a pheromone. Other
agents, pursuing their own local business, read that signal and vote with exact
`Fraction` weights. When the weighted vote crosses the quorum, the memory
migrates to where it belongs, under a cooldown that is a SQL `WHERE` clause
rather than an `if` statement.

Nobody decided this. Nobody was in charge of it. The trail formed.

And here is the part we care about most: **every state transition that changes
shared memory is sealed, in the same transaction, into a per-node hash chain,
which is committed into a chained Merkle ledger.** State change and audit entry
share one transaction or neither happens. "No unaudited transition" is not a
policy anyone has to remember. It is true by construction.

## Watch it happen — no install, no account, no signup

Open `demo/console.html` in a browser. That is the entire setup. No server, no
build step, no dependencies, works from `file://`.

Press **Run**. Three agents wake up in a field of thirty memories across three
regions — cooking, astronomy, databases — with six memories deliberately filed
in the wrong homes. Recruitment signals bloom and decay. Around eighteen
seconds in, seven signals are live. Around twenty-four seconds, the first
memory migrates. Around **forty seconds, all six misplaced memories are home.**

Then read the line the scoreboard prints:

> *7 migrations so far, 1 of them a memory the corpus never marked as misplaced
> — the neighbourhood decides, not the label.*

We did not script that. The field found a seventh memory that our own corpus
labeled as correctly filed, disagreed with us, and moved it. The mechanism does
not know what the answer key says.

The console is not a video and not a mock-up. It runs the **real decision
arithmetic** in BigInt rationals: reinforcement `c' = c + (1−c)·1/5`, hysteresis
EMA with `β=1/4` entering at `3/5` and exiting at `2/5`, consensus at
`vigor_for ≥ 1/2 · live_signals`. Same numbers, same closed forms, same exact
boundaries as the Python that runs against the cluster. It builds the audit
chain the same way too — canonical JSON, `entry_hash = sha256(prev_hash ‖
envelope)`, SHA-256 implemented inline so it works offline.

**So break it.** Click any entry in any chain, edit the payload, press *Forge*,
then press *Verify*. The page names the forgery: the sealed hash, the hash it
recomputed, and the exact sequence number where the chain stops relinking. Then
press *Restore* and watch it go green again.

## Where the floats went

Nowhere near a decision.

Every value that decides anything is an exact `Fraction`, quantized to
`DECIMAL(11,10)` only at the SQL and audit boundary, so a hash is always taken
over a canonical decimal string and never over a float. Two floating-point
exceptions exist in the entire system, both documented, both outside every
decision path:

1. **Signal decay** — `strength * exp(-rate * Δt)`, computed in SQL at read time
   over an immutable timestamp. It gates *liveness*; it never votes. The vote is
   exact; the verdict is a `Fraction` comparison.
2. **Retry backoff jitter** — deliberately random wall-clock sleep. Nothing is
   decided, hashed or verified over it.

Nothing is ever deleted, either. `FORGOTTEN` and `ORPHANED` are *states*.
Rediscovery is an audited event. The ledger only grows, which we list as an
accepted cost rather than pretend is free.

## The part we are proudest of: two verifiers that do not trust each other

A **sealed evidence bundle** is one JSON file containing everything needed to
re-examine a run without the cluster that produced it: the state tables as they
stood, every per-node hash chain, every Merkle snapshot, sealed with a hash over
its own canonical form.

    python -m demo.run_demo --dsn ... --bundle run.bundle.json
    python -m tools.verify_bundle run.bundle.json

Six checks: the bundle as shipped is the bundle as sealed; every chain relinks;
every entry hash recomputes; the content shipped is the content born; **declared
state reproduces from replaying the chains**; every Merkle snapshot recomputes.

That fifth one is the one that matters. Edit a memory's region in the file to
something the chain never authorized, and replay stops matching. *You can edit
the row; you cannot edit the evidence.*

Now the good part. Those six checks are implemented **twice** — once in
`audit/bundle.py`, once in the JavaScript inside `demo/console.html` — by two
implementations that share no code. Destroy your cluster, open the bundle in a
browser tab with nothing installed, and the page re-runs all six. If the two
ever disagree about a bundle, one of them is wrong, and we would rather find out
than not.

To prove the two canonicalizers really agree byte for byte, we ship a fixture
sealed by the Python side over deliberately hostile content: accents, CJK, a
quote, a backslash, a tab, a newline, an astral-plane emoji. It passes in the
browser. The console can also export its own run and have Python verify *that* —
the round trip closes in both directions.

## Authority: hash chains do not authorize anybody

This was the correction that came out of red-teaming. A hash chain proves what
happened. It says nothing about whether the writer was allowed to do it.

So every mutable `node_id` is bound to an authenticated CockroachDB database
principal, and every operation requires an explicit capability for the region it
touches. A revoked node cannot keep storing, reinforcing, signalling, resolving
or sweeping merely by continuing to name its old node id. One principal per
node — per agent, per Lambda, per seeder — never one connection wearing many
hats. `tools/run_authority_integration.sh` starts a disposable cluster and
proves that impersonation, cross-agent mutation and revoked writes are all
rejected, then verifies the chain and removes the cluster.

Then the boundary we did *not* solve, stated plainly: a stolen database
credential or a superuser is outside an application-level guarantee, and this is
not Byzantine consensus. Consensus dilution raises the cost of cherry-picking a
target. It is a robustness property, not a Sybil defense. `AUTHORITY_MODEL.md`
ends with the residual trust boundary rather than a victory lap.

## How CockroachDB and AWS are load-bearing

Not decorative. Remove either and the design changes.

- **Distributed vector indexing.** `VECTOR(384)` with a `region_id` prefix keeps
  semantic recall and region-scoped search inside the database. The coordination
  surface and the similarity search are the same store, which is why agents can
  coordinate without a channel.
- **Serializable transactions.** Every module takes a live cursor and never
  commits. The caller owns the transaction, so a state change and its audit
  entry are atomic together. `FOR UPDATE` on the memory row is what makes
  "two agents cannot double-migrate" a property of the database rather than a
  hope.
- **Changefeeds.** A recruitment write becomes a webhook event that triggers the
  resolver. The deployed path *reacts*; it does not poll.
- **AWS Lambda.** Two bounded workers: one resolves changefeed deliveries
  idempotently (at-least-once delivery is safe because resolution is a state
  machine), one runs scheduled signal expiry and orphan sweeping in bounded
  chunks and reports `drained: false` when work remains.

## Challenges — including the one we found today

**Making a browser tab honest.** The console has no Python and cannot run a
transformer. The temptation was to invent a similarity function and narrate
convergence anyway. Instead it uses a corpus where lexical distance is a
*truthful* signal, labels its provider on its face, and offers a bake step to
swap in the pinned model's real vectors.

That bake step is where we caught ourselves. Preparing this submission, we
tested the documented path — and found three defects stacked in it. The page had
no `<script>` tag for the vector table at all, so the README's claim that it
"picks it up automatically" was simply false. If you added the tag, the loader
fed the table's `__provider` metadata string to `atob()`, which throws and would
have killed the entire page. And if you fixed *that*, the tool baked the cluster
corpus while the console seeds its own — **one text out of thirty-nine
overlapped.** Every other lookup would have missed, every miss scores distance
1, recall would have silently collapsed to insertion order — while the page
relabeled itself as the pinned semantic model.

That last one is the exact failure `is_semantic` exists to prevent, arriving
through a build artifact instead of a provider. The fix was not just to bake
both corpora. The page now **refuses** a table that does not cover its corpus
and says so in its own provider note, with the count of missing texts, because a
partial table is worse than none. A new pure suite pins the seam, and we checked
that it actually fails against the old behaviour — a guard that cannot fail is
decoration.

**Deciding what not to build.** Region split/merge needs CAS serialization we
have not designed, so `ops/regions.py` is create-only and says so. Transitive
taint propagation is real but unbounded, so quarantine reports the one-hop
neighbourhood as advisory and never auto-flags — automatic transitive flagging
without a fixpoint bound is how a quarantine becomes a self-inflicted outage.

## What we did not do

`KNOWN_LIMITATIONS.md` is a real file and we would like you to open it. The
short version:

- **No CockroachDB Cloud cluster and no deployed Lambdas have been exercised.**
  `infra/template.json` is a reviewable SAM scaffold with static contract
  coverage and no execution evidence. It is a scaffold. We will not call it a
  deployment until `TODO.md` section 2 has output pasted into it.
- The local demo's resolver is `--local-resolver`, a labeled polling stand-in
  for the changefeed Lambda so the demo runs on a laptop.
- `demo/field_viewer.html` (custody and taint) is a staged, hand-authored
  replay. It visualizes mechanics that `audit/custody.py` really implements; it
  does not call them.
- A bundle proves what happened, not that the writer was authorized, and not
  that nothing was dropped wholesale — dropping a whole node's chain leaves an
  internally consistent file. That is caught only if a published Merkle snapshot
  already committed to that node. So publish your roots.
- Bundle v1 does not seal the per-memory custody chains.

## What we learned

That the hardest engineering discipline in a system like this is not
correctness. It is refusing to narrate certainty you have not earned.

Run the cluster demo with the non-semantic development provider and the report
exercises every mechanism, then **declines to tell you the field converged** —
because with `is_semantic = False`, distance-based claims would be fabricated.
It would have been three lines to print a convergence story anyway. Nobody would
have checked. The demo obeys its own philosophy instead, and that decision is
the reason we trust the parts that *do* make claims.

A limitation named is a decision. A limitation hidden is a bug waiting for a
better moment.

## Try it

- `demo/console.html` — open the file. Run the field, forge an entry, watch
  verification catch you. Zero setup.
- `for t in tests/test_*_pure.py; do python3 "$t"; done` — 230 assertions,
  no database required.
- `tools/run_secure_demo_local.sh` — one command: disposable CockroachDB node,
  one authenticated principal per node, a real run, a sealed bundle, verified,
  then the cluster is deleted and the evidence is not.
- Load that bundle back into the console after the cluster is gone. Then open it
  in a text editor, change one row, and watch check five refuse it.

Apache 2.0. The schema is the real spec — most invariants are constraints, not
conventions.
