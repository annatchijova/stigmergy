"""
STIGMERGY demo — N agents, no coordinator, one verifiable memory field.

    python -m demo.run_demo --dsn "postgresql://..." \
        --agents 3 --rounds 20 --provider minilm --local-resolver

What it does:
  1. SEED    — creates the three themed regions (REGION_CREATED events)
               and stores the corpus, including the deliberately
               misplaced memories (MEMORY_STORED events).
  2. AGENTS  — N threads, each its own connection and its own node_id
               (so the audit chains and the Merkle ledger have real
               multi-node content). Each round, ONE transaction:
               read own controller state -> recall (region-scoped when
               DWELLING, global when ROAMING) -> observe density ->
               reinforce the top hit -> emit a recruitment signal when
               the density gradient says a memory sits away from its
               resonant neighborhood.
  3. RESOLVE — in the deployed system this is the changefeed Lambda.
               --local-resolver runs an explicit stand-in thread that
               POLLS pending signals; it is labeled as the demo-only
               substitute it is (ARCHITECTURE.md: changefeeds, not
               polling — the flag exists so the demo runs on a laptop
               without AWS).
  4. REPORT  — tier/region distribution, where the misplaced memories
               ended up, per-node verify_chain, a fresh Merkle snapshot,
               verify_ledger. The convergence narrative prints ONLY if
               the provider is semantic (RecallResult.is_semantic);
               under the deterministic provider the report says so and
               sticks to mechanics — the demo does not fabricate the
               certainty it does not have.

Randomness note: random.choice here schedules WORKLOAD (which query an
agent asks next), simulating arrival — it never decides an outcome.
Every decision (reinforce math, consensus, hysteresis, migration) is the
same exact-arithmetic code the tests pin.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from collections import Counter
from fractions import Fraction

from audit.bundle import export_bundle, verify_bundle
from audit.chain import run_in_transaction, verify_chain
from audit.merkle import create_snapshot, verify_ledger
from ops.controller import observe, resonance_density
from ops.memories import recall, reinforce, store, verify_provider
from ops.recruitment import (
    CooldownActive, MemoryOrphaned, RegionUnavailable, emit_signal, resolve_recruitment,
)
from ops.regions import RegionExists, create_region

from .corpus import MISPLACED, QUERIES, THEMES, all_regions, region_id_for, seed_items

SEED_NODE = "demo-seeder"


def make_provider(name: str):
    if name == "minilm":
        from embeddings.minilm import MiniLMEmbeddingProvider
        return MiniLMEmbeddingProvider()
    from embeddings.deterministic import DeterministicEmbeddingProvider
    return DeterministicEmbeddingProvider()


def connect(dsn: str):
    import psycopg  # lazy — the module stays importable without the driver
    return psycopg.connect(dsn, autocommit=False)


# ---------------------------------------------------------------- seed --

def seed(dsn: str, provider) -> None:
    conn = connect(dsn)
    try:
        def make_regions(cur):
            for rid in all_regions():
                try:
                    create_region(cur, node_id=SEED_NODE, region_id=rid,
                                  locality="demo",
                                  reason="demo corpus region")
                except RegionExists:
                    pass  # re-running the demo against the same DB is fine
        run_in_transaction(conn, make_regions)

        run_in_transaction(conn, lambda cur: verify_provider(cur, provider))

        stored = 0
        for rid, text in seed_items():
            def one(cur, rid=rid, text=text):
                store(cur, provider=provider, node_id=SEED_NODE,
                      region_id=rid, content=text,
                      reason="demo corpus seed")
            run_in_transaction(conn, one)
            stored += 1
        print(f"[seed] {len(all_regions())} regions, {stored} memories "
              f"({len(MISPLACED)} deliberately misplaced), "
              f"provider={provider.provider_id}")
    finally:
        conn.close()


# --------------------------------------------------------------- agent --

def agent_round(cur, *, agent_id: str, node_id: str, provider, theme: str, query: str,
                stats: Counter) -> None:
    """One full agent round in ONE transaction (shared-cursor contract)."""
    cur.execute(
        "SELECT mode, current_region FROM agent_search_state WHERE agent_id = %s",
        (agent_id,),
    )
    row = cur.fetchone()
    mode, region = (row[0], row[1]) if row else ("ROAMING", None)

    rr = recall(cur, provider=provider, node_id=node_id,
                query_text=query,
                region_id=region if mode == "DWELLING" else None,
                limit=5)
    stats["recalls"] += 1
    stats["rediscovered"] += sum(1 for h in rr.hits if h.rediscovered)
    if not rr.hits:
        observe(cur, node_id=node_id, agent_id=agent_id,
                observation=Fraction(0))
        return

    density = resonance_density(
        (h.state, Fraction(h.confidence)) for h in rr.hits
    )

    # Where do the hits live? (a read of shared state, like everything)
    ids = [h.memory_id for h in rr.hits]
    cur.execute(
        "SELECT id, region_id FROM memories WHERE id = ANY(%s::UUID[])",
        (ids,),
    )
    region_of = {str(i): r for i, r in cur.fetchall()}
    majority_region, _ = Counter(region_of.values()).most_common(1)[0]

    res = observe(cur, node_id=node_id, agent_id=agent_id,
                  observation=density,
                  candidate_region=majority_region)
    if res.decision.transitioned:
        stats[f"mode->{res.decision.state.mode}"] += 1

    top = rr.hits[0]
    reinforce(cur, node_id=node_id, memory_id=top.memory_id,
              reason=f"top recall hit for a {theme} query")
    stats["reinforcements"] += 1

    # Stigmergic recruitment: the density gradient, not the agent,
    # says a memory sits away from its resonant neighborhood — the
    # majority region recruits the outlier.
    if region_of[top.memory_id] != majority_region:
        emit_signal(cur, node_id=node_id,
                    origin_region=majority_region,
                    memory_id=top.memory_id,
                    reason="HIGH_RESONANCE",
                    vigor=density)
        stats["signals_emitted"] += 1


def agent_loop(idx: int, dsn: str, node_id: str, provider, rounds: int, stats: Counter,
               lock: threading.Lock) -> None:
    agent_id = f"agent-{idx}"
    conn = connect(dsn)
    local = Counter()
    try:
        for _ in range(rounds):
            theme = random.choice(list(QUERIES))          # workload, not decision
            query = random.choice(QUERIES[theme])
            run_in_transaction(
                conn,
                lambda cur: agent_round(cur, agent_id=agent_id, node_id=node_id,
                                        provider=provider, theme=theme,
                                        query=query, stats=local),
            )
            time.sleep(0.05)
    finally:
        conn.close()
        with lock:
            stats.update(local)


# ------------------------------------------------- local resolver -------

def resolver_loop(dsn: str, stop: threading.Event, stats: Counter,
                  lock: threading.Lock) -> None:
    """DEMO-ONLY stand-in for the changefeed Lambda. It polls — the real
    system reacts (lambdas/changefeed_resolver.py). Labeled accordingly."""
    conn = connect(dsn)
    node_id = "demo-local-resolver"
    local = Counter()
    try:
        while not stop.is_set():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT memory_id, origin_region
                      FROM recruitment_signals
                     WHERE status = 'PENDING'
                    """
                )
                pending = cur.fetchall()
            conn.commit()
            for memory_id, origin in pending:
                try:
                    outcome = run_in_transaction(
                        conn,
                        lambda cur: resolve_recruitment(
                            cur, node_id=node_id,
                            memory_id=str(memory_id),
                            target_region=origin,
                        ),
                    )
                    local["migrations" if outcome.reached
                          else "resolutions_not_reached"] += 1
                except (CooldownActive, RegionUnavailable,
                        LookupError, ValueError, MemoryOrphaned):
                    local["resolutions_deferred"] += 1
            stop.wait(1.0)
    finally:
        conn.close()
        with lock:
            stats.update(local)


# -------------------------------------------------------------- report --

def report(dsn: str, provider, stats: Counter, bundle_path: str | None = None) -> None:
    conn = connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT region_id, tier, count(*) FROM memories "
                "GROUP BY region_id, tier ORDER BY region_id, tier"
            )
            dist = cur.fetchall()
            cur.execute("SELECT content, region_id FROM memories")
            where = {content: region for content, region in cur.fetchall()}
            cur.execute("SELECT DISTINCT node_id FROM audit_chain ORDER BY node_id")
            nodes = [r[0] for r in cur.fetchall()]
        conn.commit()

        print("\n[field] memories by region and tier:")
        for region, tier, n in dist:
            print(f"  {region:20s} {tier:10s} {n}")

        print("\n[activity]", dict(sorted(stats.items())))

        home = sum(1 for h, _s, text in MISPLACED
                   if where.get(text) == region_id_for(h))
        if provider.is_semantic:
            print(f"\n[convergence] misplaced memories now home: "
                  f"{home}/{len(MISPLACED)}")
        else:
            print(f"\n[convergence] provider {provider.provider_id!r} is "
                  "non-semantic (is_semantic=False): mechanics were "
                  "exercised, but no relevance/convergence claim is made — "
                  "the system does not fabricate certainty it does not have. "
                  "Run with --provider minilm for the convergence story.")

        print("\n[verify] per-node audit chains:")
        ok_all = True
        for node in nodes:
            v = run_in_transaction(conn, lambda cur, n=node: verify_chain(cur, n))
            ok_all &= v.ok
            print(f"  {node:22s} {'OK ' if v.ok else 'BROKEN'} "
                  f"({v.length} events) {'' if v.ok else v.detail}")

        snap = run_in_transaction(conn, create_snapshot)
        ledger = run_in_transaction(conn, verify_ledger)
        print(f"\n[merkle] snapshot seq={snap.snapshot_seq} over "
              f"{len(snap.node_chain_heads)} node chains, "
              f"root={snap.merkle_root[:16]}…")
        print(f"[merkle] ledger: "
              f"{'verified end to end' if ledger.ok else 'BROKEN: ' + ledger.detail} "
              f"({ledger.length} snapshots)")
        print("\nNo coordinator issued a single instruction. "
              "Every transition above is in a hash chain."
              if ok_all and ledger.ok else
              "\nINTEGRITY FAILURE — see details above.")

        if bundle_path:
            bundle = run_in_transaction(
                conn, lambda cur: export_bundle(cur, note="demo run"))
            with open(bundle_path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
            v = verify_bundle(bundle)
            entries = sum(len(c) for c in bundle["chains"].values())
            print(f"\n[bundle] {bundle_path} — {len(bundle['memories'])} memories, "
                  f"{len(bundle['chains'])} node chains, {entries} entries, "
                  f"{len(bundle['snapshots'])} snapshots")
            print(f"[bundle] seal {bundle['bundle_sha256']}")
            # Verify what we just wrote, before anyone is asked to believe it.
            print("[bundle] " + ("self-check VERIFIED (B1-B6)" if v.ok else
                  "SELF-CHECK FAILED: " +
                  "; ".join(f"{c.id} {c.detail}" for c in v.failed())))
            print("[bundle] open it in demo/console.html, or run "
                  f"python -m tools.verify_bundle {bundle_path}")
    finally:
        conn.close()


# ---------------------------------------------------------------- main --

def main() -> None:
    ap = argparse.ArgumentParser(description="STIGMERGY demo")
    ap.add_argument("--dsn", default=os.environ.get("STIGMERGY_DSN", ""))
    ap.add_argument("--seed-dsn", default=os.environ.get("STIGMERGY_SEED_DSN", ""))
    ap.add_argument("--agent-dsn", action="append", default=[], metavar="DSN",
                    help="one authenticated CockroachDB DSN per agent/node")
    ap.add_argument("--resolver-dsn", default=os.environ.get("STIGMERGY_RESOLVER_DSN", ""))
    ap.add_argument("--agents", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--provider", choices=("deterministic", "minilm"),
                    default="deterministic")
    ap.add_argument("--local-resolver", action="store_true",
                    help="run the polling stand-in for the changefeed Lambda")
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--bundle", metavar="PATH",
                    help="write a sealed evidence bundle of this run")
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("--dsn or STIGMERGY_DSN is required for reporting.")
    seed_dsn = args.seed_dsn or args.dsn
    resolver_dsn = args.resolver_dsn or args.dsn
    if args.agent_dsn and len(args.agent_dsn) != args.agents:
        raise SystemExit("--agent-dsn must be supplied exactly once per --agents value.")
    if not args.agent_dsn:
        raise SystemExit(
            "secure multi-node demo requires one --agent-dsn per agent; "
            "each must authenticate as that agent's registered DB principal."
        )

    provider = make_provider(args.provider)
    if not args.skip_seed:
        seed(seed_dsn, provider)

    stats: Counter = Counter()
    lock = threading.Lock()
    stop = threading.Event()

    resolver = None
    if args.local_resolver:
        resolver = threading.Thread(
            target=resolver_loop, args=(resolver_dsn, stop, stats, lock),
            daemon=True)
        resolver.start()

    threads = [
        threading.Thread(target=agent_loop,
                         args=(i, args.agent_dsn[i], f"agent-{i}", provider,
                               args.rounds, stats, lock))
        for i in range(args.agents)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if resolver is not None:
        time.sleep(2.0)  # let in-flight signals resolve
        stop.set()
        resolver.join(timeout=5.0)

    report(args.dsn, provider, stats, bundle_path=args.bundle)


if __name__ == "__main__":
    main()
