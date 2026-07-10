"""
STIGMERGY — Chained Merkle ledger over all nodes' chain heads (Invariant 5).

No global ordering of events exists — deliberately. What DOES exist is a
periodic, chained commitment to the state of every per-node chain:

    leaves       = sha256(node_id || ":" || head_hash) for each node,
                   node_id ASC                     (canonical leaf order)
    merkle_root  = standard binary Merkle tree over leaves; odd leaf is
                   promoted unpaired (no duplication — duplicating the
                   last leaf, Bitcoin-style, admits two leaf sets with
                   one root, which is an ambiguity we refuse)
    ledger_hash  = sha256(parent_ledger_hash || merkle_root)
    genesis      : parent_ledger_hash = sha256(b"STIGMERGY_LEDGER_GENESIS"),
                   snapshot_seq = 0 (deterministic, schema-enforced >= 0)

UNIQUE(parent_snapshot) + UNIQUE(snapshot_seq) in the schema make ledger
forks a constraint violation. This module makes honest snapshots; the
schema makes dishonest ones impossible to commit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .canonical import canonical_json

LEDGER_GENESIS_HASH = hashlib.sha256(b"STIGMERGY_LEDGER_GENESIS").hexdigest()


def _leaf(node_id: str, head_hash: str) -> str:
    return hashlib.sha256(f"{node_id}:{head_hash}".encode("utf-8")).hexdigest()


def merkle_root_over_heads(heads: dict[str, str]) -> str:
    """
    Deterministic Merkle root over {node_id: head_entry_hash}.
    Leaves ordered by node_id ASC — the same canonical ordering the
    stored node_chain_heads JSON uses, one discipline in one place.
    """
    if not heads:
        raise ValueError("Cannot snapshot an empty set of chain heads.")
    for nid, head in heads.items():
        if not isinstance(nid, str) or not isinstance(head, str):
            raise TypeError(
                f"Chain head ({nid!r}: {head!r}) is not a str/str pair — "
                "the hash would silently cover str(x) while storage kept "
                "the original type. Refusing the inconsistency."
            )
    level = [_leaf(nid, heads[nid]) for nid in sorted(heads)]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(hashlib.sha256((level[i] + level[i + 1]).encode("utf-8")).hexdigest())
        if len(level) % 2 == 1:
            nxt.append(level[-1])  # promote unpaired leaf, never duplicate
        level = nxt
    return level[0]


@dataclass(frozen=True)
class Snapshot:
    snapshot_seq: int
    parent_snapshot: str | None
    node_chain_heads: dict[str, str]
    merkle_root: str
    ledger_hash: str


def create_snapshot(cur) -> Snapshot:
    """
    Take one snapshot of every node's current chain head, chained to the
    previous snapshot. Runs inside the caller's transaction (same
    contract as append_event — this module never commits).

    The FOR UPDATE on the current ledger head serializes concurrent
    snapshotters; if two race anyway, UNIQUE(snapshot_seq) turns the
    loser into a retryable constraint error instead of a fork.
    """
    cur.execute(
        """
        SELECT DISTINCT ON (node_id) node_id, entry_hash
          FROM audit_chain
         ORDER BY node_id, seq DESC
        """
    )
    heads = {node_id: entry_hash for node_id, entry_hash in cur.fetchall()}
    if not heads:
        raise ValueError(
            "No audit chains exist yet — a snapshot of nothing is not a "
            "snapshot, and fabricating one would be manufactured certainty."
        )

    cur.execute(
        """
        SELECT snapshot_id, snapshot_seq, ledger_hash
          FROM merkle_snapshots
         ORDER BY snapshot_seq DESC
         LIMIT 1
           FOR UPDATE
        """
    )
    head = cur.fetchone()
    if head is None:
        parent_id, seq, parent_ledger_hash = None, 0, LEDGER_GENESIS_HASH
    else:
        parent_id, seq, parent_ledger_hash = head[0], head[1] + 1, head[2]

    root = merkle_root_over_heads(heads)
    ledger_hash = hashlib.sha256((parent_ledger_hash + root).encode("utf-8")).hexdigest()
    heads_canonical = canonical_json(heads)  # dict[str,str]: sorted keys, exact

    cur.execute(
        """
        INSERT INTO merkle_snapshots
            (parent_snapshot, snapshot_seq, node_chain_heads,
             merkle_root, ledger_hash, created_at)
        VALUES (%s, %s, %s::JSONB, %s, %s, %s)
        """,
        (parent_id, seq, heads_canonical, root, ledger_hash,
         datetime.now(timezone.utc)),
    )

    return Snapshot(
        snapshot_seq=seq,
        parent_snapshot=str(parent_id) if parent_id is not None else None,
        node_chain_heads=heads,
        merkle_root=root,
        ledger_hash=ledger_hash,
    )


@dataclass(frozen=True)
class LedgerVerification:
    ok: bool
    length: int
    first_broken_seq: int | None
    detail: str


def verify_ledger(cur) -> LedgerVerification:
    """
    Walk the snapshot ledger from genesis: recompute every merkle_root
    from its stored heads, recompute every ledger_hash from its parent,
    verify parent linkage and seq continuity. First broken seq reported;
    everything before it stands, nothing after it does.
    """
    cur.execute(
        """
        SELECT snapshot_id, parent_snapshot, snapshot_seq,
               node_chain_heads, merkle_root, ledger_hash
          FROM merkle_snapshots
         ORDER BY snapshot_seq ASC
        """
    )
    rows = cur.fetchall()
    if not rows:
        return LedgerVerification(True, 0, None, "empty ledger (vacuously valid)")

    expected_parent_hash = LEDGER_GENESIS_HASH
    expected_parent_id = None
    for i, (sid, parent_id, seq, heads_json, root, ledger_hash) in enumerate(rows):
        if seq != i:
            return LedgerVerification(False, len(rows), seq,
                                      f"sequence gap: expected {i}, found {seq}")
        if (parent_id is None) != (expected_parent_id is None) or (
            parent_id is not None and str(parent_id) != str(expected_parent_id)
        ):
            return LedgerVerification(False, len(rows), seq,
                                      "parent_snapshot does not match previous snapshot_id")
        heads = heads_json if isinstance(heads_json, dict) else json.loads(heads_json)
        if not isinstance(heads, dict):
            return LedgerVerification(False, len(rows), seq,
                                      f"node_chain_heads is not a JSON object: got {type(heads).__name__}")
        # NOTE: dict order here is irrelevant by construction —
        # merkle_root_over_heads sorts node_ids internally. Determinism
        # rests on that explicit sorted(), not on parser behavior.
        recomputed_root = merkle_root_over_heads(heads)
        if recomputed_root != root:
            return LedgerVerification(False, len(rows), seq,
                                      "merkle_root does not match stored node_chain_heads")
        recomputed_ledger = hashlib.sha256(
            (expected_parent_hash + recomputed_root).encode("utf-8")
        ).hexdigest()
        if recomputed_ledger != ledger_hash:
            return LedgerVerification(False, len(rows), seq,
                                      "ledger_hash chain broken")
        expected_parent_hash = ledger_hash
        expected_parent_id = sid

    return LedgerVerification(True, len(rows), None, "ledger verified end to end")
