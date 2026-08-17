# STIGMERGY — demo runbook

Everything needed to record the submission video, in the order it happens, with
timings measured rather than guessed. The one rule inherited from the rest of the
project: **never show something labeled as more than it is.** A judge who catches
one overclaim stops believing the verified parts too.

## Before you press record

    for t in tests/test_*_pure.py; do python3 "$t"; done   # 230 assertions, ~seconds

Two paths exist. Record **both** — they answer different questions.

| | `demo/console.html` | `tools/run_secure_demo_local.sh` |
|---|---|---|
| needs | a browser, nothing else | CockroachDB v25.2+, `psycopg` |
| shows | the decision arithmetic and the audit construction, live and tamperable | a real cluster, real principals, real changefeed-shaped resolution |
| is | a simulation of the field, exact in its math | the actual system |
| answers | "is the verification real?" | "does it run?" |

### Path A — the console (zero setup, this always works)

    open demo/console.html          # or: xdg-open / drag it into a tab

No server, no build, no dependencies. Measured on a cold load:

| you do | you see | when |
|---|---|---|
| press **Run** | agents roam, recall, reinforce; signals appear | immediately |
| — | signals peak around 7 live | ~t+18s |
| — | **first migration** (cooldown is compressed to 8s) | ~t+24s |
| — | **6 / 6 misplaced memories home** | ~t+39s |

Let it run the full ~40 seconds. The scoreboard ends with a line worth reading
aloud: *"7 migrations so far, 1 of them a memory the corpus never marked as
misplaced — the neighbourhood decides, not the label."* That is the whole thesis
in one sentence, and it was not scripted — the field did it.

Then the tamper beat, in this order (the button is deliberately explicit):

1. **Click an entry** in a node's chain. The button reads *"Forge the selected
   entry"* — with nothing selected it answers *"Pick an entry first."* on screen,
   which is a stumble on camera. Select first.
2. Edit the payload, press **Forge**.
3. Press **Verify**. It names the forgery: the sealed hash, the recomputed hash,
   and the sequence number where the chain stops relinking.
4. Press **Restore**, verify again — clean. After a ~40 s run that is around 460
   entries across 5 node chains recomputing exactly; the count depends on how
   long you let it run, the outcome does not.

### Path B — the cluster (the run that produces evidence)

    pip install "psycopg[binary]" sentence-transformers
    tools/run_secure_demo_local.sh

It starts a disposable localhost CockroachDB node, applies `schema.sql`, creates
**one authenticated database principal per node** (seeder, each agent, resolver —
not one shared connection wearing many hats), performs the trusted bootstrap,
runs the field, verifies every chain, takes a Merkle snapshot, verifies the
ledger, **exports a sealed evidence bundle**, verifies that bundle with the
independent Python verifier, and then deletes the cluster.

The script announces its provider and auto-detects it. Without
`sentence-transformers` it runs `deterministic`, which exercises every mechanism
and then **refuses to narrate convergence** (`is_semantic = False`) — correct
behaviour, confusing footage. Install the model for the take you keep.

Say the word "disposable" out loud. This is a local cluster, not CockroachDB
Cloud, and the resolver is `--local-resolver`, a labeled polling stand-in for the
changefeed Lambda. Both facts are in the output; read them rather than let a
judge discover them.

### The bridge between the two paths — record this

This is the strongest thirty seconds available, because it is the only part no
demo can fake:

1. The cluster run wrote `run.bundle.json`. The cluster is now **deleted**.
2. Open `demo/console.html` → **Open a sealed bundle** → pick that file.
3. The page renders the real run and executes the same six checks in JavaScript
   that `audit/bundle.py` just executed in Python. Two independent
   implementations, one of them a browser tab with nothing installed.
4. Open the bundle in a text editor. Change one memory's `region_id` to a region
   the chain never authorized. Reload. **B5 fails** — declared state no longer
   replays from the chain. You can edit the row; you cannot edit the evidence.

`tests/fixtures/cross_impl.bundle.json` is the companion beat: sealed by the
Python canonicalizer over deliberately awkward content (accents, CJK, a quote, a
backslash, a tab, a newline, an astral-plane emoji). B1 passing in the browser is
what proves the two canonicalizers agree byte for byte.

## A three-minute cut

| time | on screen | the sentence that carries it |
|---|---|---|
| 0:00 | the three regions, six memories in the wrong homes | "Nothing here coordinates these agents." |
| 0:20 | **Run** — traces, signals, votes | "They never talk. They read and write one database." |
| 1:00 | 6/6, including the unlabeled one | "The neighbourhood decided, not the label." |
| 1:20 | forge → **Verify** names it → restore | "Every step is sealed. Here is what happens when I lie." |
| 1:50 | `run_secure_demo_local.sh` — real cluster, per-node principals | "Same arithmetic, real CockroachDB, one principal per node." |
| 2:20 | bundle loaded into the console after the cluster is gone | "Two independent verifiers. Edit the row, the evidence says no." |
| 2:45 | `KNOWN_LIMITATIONS.md` on screen | "And here is everything this does not prove." |

Ending on the limitations file is not modesty. It is the most persuasive frame
available: a system that names its own boundaries is a system whose claims you
can check.

## Optional: swap in the pinned model's real vectors

The console ships without a vector table and says so on its face — recall runs a
labeled tf-idf cosine over a corpus written to make lexical distance truthful.
To run the console on the **same** `all-MiniLM-L6-v2` embeddings the cluster
pins:

    pip install sentence-transformers
    python -m tools.bake_embeddings        # writes demo/minilm_vectors.js

The page picks it up on reload and relabels itself. It also **refuses** a table
that does not cover its corpus, and says why on the page: a partial table would
score distance 1 on every miss and collapse recall to insertion order while
claiming the pinned model. Full coverage or the labeled fallback; nothing in
between.

One cosmetic consequence, so it does not surprise you if a judge opens devtools:
on a clone without the table, the browser logs a single
`ERR_FILE_NOT_FOUND` for `minilm_vectors.js`. The tag is a deliberate optional
load — the page checks for the table and demotes itself when it is absent. The
run is unaffected, and nothing in the page reports an error.

## What is not in this demo, and must not be implied by it

Read `TODO.md` for the tracked version. In one breath, for the video:

- No **CockroachDB Cloud** cluster and no **deployed AWS Lambdas** have been
  exercised. `infra/template.json` has static contract coverage and no execution
  evidence. Say "scaffold", never "deployed".
- The changefeed → Lambda path is not in either recording. The demo resolver
  polls, and is labeled where it appears.
- `demo/field_viewer.html` (custody + taint) is a **staged, hand-authored
  replay**. If it appears on camera, say so in the same sentence.
- A bundle proves what happened, not that the writer was authorized. Authority
  is enforced at the database and leaves no artifact a detached file can
  re-check.
