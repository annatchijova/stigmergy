"""
Sealed evidence bundle: export a run, verify it anywhere.

A bundle is one JSON document holding everything needed to re-examine a run
without the cluster that produced it: the state tables as they stood, every
per-node hash chain, and every Merkle snapshot. It is sealed with a hash over
its own canonical form, so the file that ships is provably the file that was
sealed.

The verifier here takes a bundle and NOTHING else — no cursor, no network, no
trust in the exporter. demo/console.html reimplements the same six checks in
JavaScript, so a bundle can be verified by two independent implementations, one
of which runs in a browser tab with no install.

What a bundle proves
--------------------
That the evidence it contains is internally consistent: a payload edited after
the fact stops reproducing its entry hash, a removed entry breaks the linkage,
and a state column edited to something the chain never authorized stops
replaying.

What a bundle does NOT prove
----------------------------
1. That the writer was AUTHORIZED. Authority is enforced at the database
   (AUTHORITY_MODEL.md) and leaves no artifact a detached file can re-check.
   A bundle shows what happened, not that it was allowed.
2. That nothing was omitted WHOLESALE. Dropping a node's chain entirely leaves
   an internally consistent bundle. The defence is B6: if a Merkle snapshot
   covering that node was published earlier, the omission shows up as a root
   that no longer recomputes. Publish your roots.
3. Anything about the per-memory custody chains (audit/custody.py). Those have
   their own verifier and are out of scope for bundle version 1.

Stating those three is the point. A verifier that implies more than it checks
is worse than no verifier.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction

from .canonical import canonical_json, quantize
from .chain import GENESIS_PREV_HASH, _format_ts, compute_entry_hash
from .merkle import merkle_root_over_heads

BUNDLE_VERSION = 1
GENERATOR = "stigmergy/audit.bundle"

# The seal covers every key except the seal itself.
SEAL_KEY = "bundle_sha256"


def seal_of(body: dict) -> str:
    """sha256 over the bundle's canonical form, seal key excluded."""
    payload = {k: v for k, v in body.items() if k != SEAL_KEY}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _dec(value) -> str:
    """Every number that crosses the boundary leaves as a fixed-point string at
    canonical scale — the same representation the audit payloads use, so a
    bundle never carries two spellings of one value."""
    if value is None:
        return None
    if isinstance(value, float):
        raise TypeError("float reached the bundle boundary; quantize first.")
    return format(quantize(value if isinstance(value, (Decimal, Fraction, int))
                           else Decimal(str(value))), "f")


# ------------------------------------------------------------------ export --

def export_bundle(cur, *, note: str | None = None) -> dict:
    """Read a whole run out of a live cursor and seal it. Read-only."""
    cur.execute(
        "SELECT region_id, locality, status, generation FROM memory_regions "
        "ORDER BY region_id"
    )
    regions = [
        {"region_id": r[0], "locality": r[1], "status": r[2], "generation": int(r[3])}
        for r in cur.fetchall()
    ]

    cur.execute(
        "SELECT id, region_id, content, content_sha256, state, tier, confidence, "
        "custody_status FROM memories ORDER BY id"
    )
    memories = [
        {
            "memory_id": str(m[0]), "region_id": m[1], "content": m[2],
            "content_sha256": m[3], "state": m[4], "tier": m[5],
            "confidence": _dec(m[6]), "custody_status": m[7],
        }
        for m in cur.fetchall()
    ]

    cur.execute(
        "SELECT id, memory_id, origin_region, vigor, signal_strength, decay_rate, "
        "status, created_at, expires_at FROM recruitment_signals ORDER BY id"
    )
    signals = [
        {
            "signal_id": str(s[0]), "memory_id": str(s[1]), "origin_region": s[2],
            "vigor": _dec(s[3]), "signal_strength": _dec(s[4]), "decay_rate": _dec(s[5]),
            "status": s[6], "created_at": _format_ts(s[7]), "expires_at": _format_ts(s[8]),
        }
        for s in cur.fetchall()
    ]

    cur.execute(
        "SELECT node_id, seq, prev_hash, entry_hash, event_type, reason, "
        "payload_json, created_at FROM audit_chain ORDER BY node_id, seq"
    )
    chains: dict[str, list[dict]] = {}
    for node_id, seq, prev_hash, entry_hash, event_type, reason, payload, created_at in cur.fetchall():
        chains.setdefault(node_id, []).append({
            "seq": int(seq), "prev_hash": prev_hash, "entry_hash": entry_hash,
            "event_type": event_type, "reason": reason,
            "created_at": _format_ts(created_at),
            # Re-canonicalized from the stored JSONB. chain.py already relies on
            # canonical_json being a function of the VALUE, not of whatever
            # spelling happened to reach storage.
            "payload": payload,
        })

    cur.execute(
        "SELECT snapshot_seq, node_chain_heads, merkle_root, ledger_hash, created_at "
        "FROM merkle_snapshots ORDER BY snapshot_seq"
    )
    snapshots = [
        {
            "snapshot_seq": int(s[0]), "node_chain_heads": s[1], "merkle_root": s[2],
            "ledger_hash": s[3], "created_at": _format_ts(s[4]),
        }
        for s in cur.fetchall()
    ]

    body = {
        "bundle_version": BUNDLE_VERSION,
        "generator": GENERATOR,
        "note": note or "",
        "regions": regions,
        "memories": memories,
        "signals": signals,
        "chains": chains,
        "snapshots": snapshots,
    }
    body[SEAL_KEY] = seal_of(body)
    return body


# ------------------------------------------------------------------ verify --

@dataclass
class Check:
    id: str
    claim: str
    ok: bool = True
    detail: str = ""


@dataclass
class BundleVerification:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


CHECK_CLAIMS = [
    ("B1", "the bundle as shipped is the bundle as sealed"),
    ("B2", "every chain relinks: genesis, dense sequence, prev_hash linkage"),
    ("B3", "every entry hash recomputes from its stored payload"),
    ("B4", "the content shipped is the content born"),
    ("B5", "declared memory state reproduces from replaying the chains"),
    ("B6", "every Merkle snapshot recomputes over its recorded heads"),
]

# Only these event types move the state B5 replays; anything else is
# information, not a transition, and is deliberately ignored.
_REPLAYED = {"MEMORY_STORED", "MEMORY_REINFORCED", "MEMORY_MIGRATED",
             "REDISCOVERED", "MEMORIES_ORPHANED"}


def _ordered_entries(chains: dict) -> list[tuple]:
    """Every entry across every node, in one canonical order: timestamp first,
    then node_id and seq so ties resolve the same way for every verifier."""
    out = []
    for node_id in sorted(chains):
        for e in chains[node_id]:
            out.append((e.get("created_at", ""), node_id, e.get("seq", 0), e))
    out.sort(key=lambda t: (t[0], t[1], t[2]))
    return out


def replay_memory_state(chains: dict) -> dict[str, dict]:
    """The state the evidence says the memories must be in."""
    state: dict[str, dict] = {}
    for _ts, _node, _seq, e in _ordered_entries(chains):
        et, p = e.get("event_type"), e.get("payload") or {}
        if et not in _REPLAYED:
            continue
        if et == "MEMORY_STORED":
            state[p["memory_id"]] = {
                "region_id": p["region_id"], "confidence": p["confidence"],
                "state": "NEUTRAL", "tier": "SHORT_TERM",
            }
        elif et == "MEMORY_REINFORCED":
            m = state.get(p["memory_id"])
            if m is not None:
                m["state"] = p["new_state"]
                m["confidence"] = p["new_confidence"]
        elif et == "MEMORY_MIGRATED":
            m = state.get(p["memory_id"])
            if m is not None:
                m["region_id"] = p["target_region"]
                m["tier"] = p["new_tier"]
        elif et == "REDISCOVERED":
            m = state.get(p["memory_id"])
            if m is not None:
                m["tier"] = p["new_tier"]
        elif et == "MEMORIES_ORPHANED":
            for o in p.get("orphaned", []):
                m = state.get(o["memory_id"])
                if m is not None:
                    m["tier"] = "ORPHANED"
    return state


def verify_bundle(bundle: dict) -> BundleVerification:
    """Six checks over a bundle and nothing else. No cursor, no network."""
    checks = {cid: Check(cid, claim) for cid, claim in CHECK_CLAIMS}

    def fail(cid: str, detail: str) -> None:
        if checks[cid].ok:                      # keep the FIRST failure per check
            checks[cid].ok = False
            checks[cid].detail = detail

    # ---- B1: the seal
    declared = bundle.get(SEAL_KEY)
    try:
        recomputed = seal_of(bundle)
    except (TypeError, ValueError) as exc:
        recomputed = None
        fail("B1", f"bundle is not canonicalizable: {exc}")
    if recomputed is not None and declared != recomputed:
        fail("B1", f"declared {declared}, recomputed {recomputed}")

    chains = bundle.get("chains") or {}

    # ---- B2 / B3: the chains
    for node_id in sorted(chains):
        expected_prev = GENESIS_PREV_HASH
        for i, e in enumerate(chains[node_id]):
            if e.get("seq") != i:
                fail("B2", f"{node_id}: expected seq {i}, found {e.get('seq')}")
                break
            if e.get("prev_hash") != expected_prev:
                fail("B2", f"{node_id} seq {i}: prev_hash does not link to the entry before it")
                break
            try:
                recomputed, _ = compute_entry_hash(
                    node_id=node_id, seq=e["seq"], prev_hash=e["prev_hash"],
                    event_type=e["event_type"], reason=e["reason"],
                    created_at=_parse_ts(e["created_at"]), payload=e["payload"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                fail("B3", f"{node_id} seq {i}: entry is not hashable ({exc})")
                break
            if recomputed != e.get("entry_hash"):
                fail("B3", f"{node_id} seq {i}: sealed {e.get('entry_hash')}, "
                           f"recomputed {recomputed}")
                break
            expected_prev = e["entry_hash"]

    # ---- B4: content integrity
    for m in bundle.get("memories") or []:
        digest = m.get("content_sha256")
        if digest is None:
            continue                            # column is nullable by schema
        actual = hashlib.sha256((m.get("content") or "").encode("utf-8")).hexdigest()
        if actual != digest:
            fail("B4", f"{m.get('memory_id')}: content does not hash to its recorded digest")

    # ---- B5: state must be derivable from evidence
    replayed = replay_memory_state(chains)
    for m in bundle.get("memories") or []:
        mid = m.get("memory_id")
        r = replayed.get(mid)
        if r is None:
            fail("B5", f"{mid}: no MEMORY_STORED in the bundle — its state is not derivable")
            continue
        for key in ("region_id", "state", "tier", "confidence"):
            if m.get(key) != r[key]:
                fail("B5", f"{mid}: declared {key}={m.get(key)!r}, "
                           f"replay says {r[key]!r}")
                break

    # ---- B6: the ledger
    heads_now = {n: chains[n][-1]["entry_hash"] for n in chains if chains[n]}
    for s in bundle.get("snapshots") or []:
        heads = s.get("node_chain_heads") or {}
        try:
            root = merkle_root_over_heads(heads)
        except (TypeError, ValueError) as exc:
            fail("B6", f"snapshot {s.get('snapshot_seq')}: heads unusable ({exc})")
            continue
        if root != s.get("merkle_root"):
            fail("B6", f"snapshot {s.get('snapshot_seq')}: declared root "
                       f"{s.get('merkle_root')}, recomputed {root}")
            continue
        for node_id, head in heads.items():
            if node_id not in chains:
                fail("B6", f"snapshot {s.get('snapshot_seq')} commits to node "
                           f"{node_id!r}, which the bundle does not contain")
            elif head not in {e["entry_hash"] for e in chains[node_id]} and head != heads_now.get(node_id):
                fail("B6", f"snapshot {s.get('snapshot_seq')}: head {head[:16]}… for "
                           f"{node_id} appears nowhere in that node's chain")

    return BundleVerification([checks[cid] for cid, _ in CHECK_CLAIMS])


def _parse_ts(text: str):
    from datetime import datetime
    return datetime.fromisoformat(text)
