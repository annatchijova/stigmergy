"""
STIGMERGY — Per-MEMORY tamper-evident hash chain (custody layer).

Ported from MNEME (github.com/annatchijova/mneme), whose schema.sql says
outright it was "written to port to CockroachDB with mechanical changes
only" — this module is that port.

audit.chain answers "what did this NODE do, in order" (Invariant 5:
audit_chain is per-node). This module answers a different question:
"what happened to THIS MEMORY, in order — who created it, who reinforced
it, what contradicted it, when was it quarantined and why, can I prove
none of that history was rewritten after the fact." Neither chain
replaces the other; they are additive, same discipline, different axis.

Design decisions, each load-bearing (identical reasoning to MNEME's
custody.py, restated for this codebase):

  GENESIS IS BOUND TO THE MEMORY ID, NOT A GLOBAL CONSTANT.
      prev_hash of seq 0 is sha256(b"MNEME_CUSTODY_GENESIS:" || memory_id).
      audit.chain's genesis is a global constant because its chains are
      per-node and node_id already sits inside every hashed envelope;
      here the binding moves into genesis so verifying one exported
      chain needs nothing but the chain and the memory_id it claims to
      describe — grafting memory A's history onto memory B fails at
      seq 0 by construction. (The constant's literal value is kept
      identical to MNEME's on purpose: a future cross-project verifier
      recognizes the scheme for free. This costs nothing now.)

  ACTOR IDENTITY IS INSIDE EVERY HASH.
      Every event names actor_id — here, a node_id from agent_nodes,
      reusing STIGMERGY's existing identity system rather than inventing
      a second one (MNEME had a standalone `actors` table only because
      it had no authority layer of its own). Tampering with attribution
      is tampering with the hash, which is what makes ops.trust's taint
      propagation a verifiable set rather than a log grep.

  REASON IS NOT NULL — an unreasoned custody event cannot exist.
      Same discipline as audit.chain's Invariant 3.

  THE TRANSACTION BELONGS TO THE CALLER.
      append_event() takes a live cursor and never commits — identical
      contract to audit.chain.append_event. The state change (memories
      table, cell_links, custody_status) and its custody event land in
      the same transaction or not at all.

Two behavioral adaptations from MNEME's SQLite version (not cosmetic):

  1. The head-read locks with `FOR UPDATE`, matching audit.chain's
     concurrency contract under CockroachDB serializable isolation.
     MNEME has no concurrent writers to serialize against (SQLite,
     single process) so its version has no lock; STIGMERGY does.
  2. created_at is a timezone-aware datetime end-to-end, formatted only
     at hash time via _format_ts (copied verbatim from audit.chain) and
     stored directly into a TIMESTAMPTZ column — not a pre-formatted
     string, which MNEME uses only because SQLite has no timestamp type.

Hash formula (identical shape to audit.chain.compute_entry_hash, with
memory_id + actor_id in the envelope instead of node_id alone):

    entry_hash = sha256(
        prev_hash
        || canonical_json({
             "memory_id":  memory_id,
             "seq":        seq,
             "event_type": event_type,
             "actor_id":   actor_id,
             "reason":     reason,
             "created_at": <canonical UTC microsecond ISO 8601>,
             "payload":    payload,
           })
    )                                              -> hex digest
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .canonical import canonical_json

# Kept byte-identical to MNEME's constant — see module docstring.
_GENESIS_PREFIX = b"MNEME_CUSTODY_GENESIS:"

# Closed vocabulary, narrower than MNEME's: no 'SUPERSEDED_BY' (no
# supersede() flow exists in this codebase — see KNOWN_LIMITATIONS.md).
# 'STATE_CHANGED' is kept reserved-but-unused, documented here so its
# absence of a caller reads as a decision, not an oversight.
EVENT_TYPES = frozenset(
    {
        "STORED",           # birth; payload MUST carry content_sha256
        "REINFORCED",       # confidence raised (mirrors MEMORY_REINFORCED)
        "CONTRADICTED_BY",  # another memory conflicts; payload names it
        "QUARANTINED",      # direct action against this memory
        "TAINT_FLAGGED",    # transitive: an actor in this chain was quarantined
        "REHABILITATED",    # audited reversal of TAINT_FLAGGED
        "STATE_CHANGED",    # reserved; no caller yet
    }
)


def genesis_hash(memory_id: str) -> str:
    """
    Per-memory genesis. Binding memory_id into seq 0's prev_hash is what
    makes chain grafting (custody of A presented as custody of B)
    structurally impossible rather than merely detectable after export.
    """
    if not isinstance(memory_id, str) or not memory_id:
        raise ValueError("memory_id must be a non-empty string.")
    return hashlib.sha256(_GENESIS_PREFIX + memory_id.encode("utf-8")).hexdigest()


def _format_ts(ts: datetime) -> str:
    """
    The single canonical timestamp formatter — copied verbatim from
    audit.chain._format_ts so both chains share one notion of "canonical
    UTC" and a round trip through TIMESTAMPTZ cannot introduce drift
    between them.
    """
    if ts.tzinfo is None:
        raise ValueError("Naive datetime in custody chain — timestamps must be timezone-aware UTC.")
    return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")


def content_sha256(content: str) -> str:
    """Content identity. UTF-8 bytes, nothing clever."""
    if not isinstance(content, str):
        raise TypeError("Memory content must be str.")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_entry_hash(
    *,
    memory_id: str,
    seq: int,
    prev_hash: str,
    event_type: str,
    actor_id: str,
    reason: str,
    created_at: datetime,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """
    Returns (entry_hash, canonical_payload_str) — mirrors
    audit.chain.compute_entry_hash's contract exactly, so the caller
    stores the EXACT bytes that were hashed.
    """
    canonical_payload = canonical_json(payload)
    envelope = canonical_json(
        {
            "memory_id": memory_id,
            "seq": seq,
            "event_type": event_type,
            "actor_id": actor_id,
            "reason": reason,
            "created_at": _format_ts(created_at),
            "payload": json.loads(canonical_payload),
        }
    )
    entry_hash = hashlib.sha256((prev_hash + envelope).encode("utf-8")).hexdigest()
    return entry_hash, canonical_payload


@dataclass(frozen=True)
class CustodyEntry:
    memory_id: str
    seq: int
    prev_hash: str
    entry_hash: str
    event_type: str
    actor_id: str
    reason: str
    created_at: datetime


def append_event(
    cur,
    *,
    memory_id: str,
    event_type: str,
    actor_id: str,
    reason: str,
    payload: dict[str, Any],
) -> CustodyEntry:
    """
    Append one custody event for memory_id, INSIDE THE CALLER'S
    TRANSACTION. Contract identical to audit.chain.append_event: `cur`
    is a psycopg cursor on an open transaction; this function performs
    reads and one INSERT, never commits. Use audit.chain.run_in_transaction
    to drive the caller's transaction (this module does not reimplement it).

    Enforcement, with our words, before SQL can object with its own:
      - event_type must be in the closed vocabulary;
      - reason must be non-empty (an unreasoned event cannot exist);
      - STORED must be seq 0 and must carry content_sha256;
      - any non-STORED event on a chain with no head is refused —
        custody begins at birth, not at first incident;
      - a memory already born cannot be STORED again (birth happens once).
    """
    if not reason or not reason.strip():
        raise ValueError("Custody events require a non-empty reason.")
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"Unknown custody event_type {event_type!r}. The vocabulary is "
            "closed; extending it is a protocol change, not a call-site choice."
        )

    cur.execute(
        """
        SELECT seq, entry_hash
          FROM custody_chain
         WHERE memory_id = %s
         ORDER BY seq DESC
         LIMIT 1
           FOR UPDATE
        """,
        (memory_id,),
    )
    head = cur.fetchone()

    if head is None:
        if event_type != "STORED":
            raise ValueError(
                f"First custody event for {memory_id} must be STORED, got "
                f"{event_type}. Custody begins at birth."
            )
        seq, prev_hash = 0, genesis_hash(memory_id)
    else:
        if event_type == "STORED":
            raise ValueError(
                f"{memory_id} already has a custody chain; STORED is a birth "
                "event and a memory is born once."
            )
        seq, prev_hash = head[0] + 1, head[1]

    if event_type == "STORED":
        csha = payload.get("content_sha256")
        if not (isinstance(csha, str) and len(csha) == 64):
            raise ValueError(
                "STORED payload must carry content_sha256 (64 hex chars). "
                "A birth event that does not seal the content seals nothing."
            )

    created_at = datetime.now(timezone.utc)
    entry_hash, canonical_payload = compute_entry_hash(
        memory_id=memory_id,
        seq=seq,
        prev_hash=prev_hash,
        event_type=event_type,
        actor_id=actor_id,
        reason=reason,
        created_at=created_at,
        payload=payload,
    )

    cur.execute(
        """
        INSERT INTO custody_chain
            (memory_id, seq, prev_hash, entry_hash, event_type, actor_id,
             reason, payload_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s)
        """,
        (memory_id, seq, prev_hash, entry_hash, event_type, actor_id,
         reason, canonical_payload, created_at),
    )

    return CustodyEntry(
        memory_id=memory_id,
        seq=seq,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        event_type=event_type,
        actor_id=actor_id,
        reason=reason,
        created_at=created_at,
    )


def verify_custody_rows(memory_id: str, rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """
    Verify a full custody chain given its rows in ASCENDING seq order.
    Pure — no cursor — same shape as audit.chain.verify_chain but walking
    a per-memory chain. A future offline verifier (out of scope for this
    port) can share this implementation, same as MNEME's own.

    Checks, in the order a lie breaks first:
      1. non-empty; first event STORED, STORED only once
      2. seq dense from 0
      3. genesis binding: rows[0].prev_hash == genesis_hash(memory_id)
      4. linkage: rows[i].prev_hash == rows[i-1].entry_hash
      5. vocabulary: every event_type is known
      6. recomputation: every entry_hash re-derives from its fields
      7. created_at is canonical UTC form AND non-decreasing along seq —
         a custody chain cannot run backwards in time.
    """
    if not rows:
        return False, [f"{memory_id}: empty custody chain — a memory without a birth event."]

    expected_prev = genesis_hash(memory_id)
    prev_ts: str | None = None
    for i, r in enumerate(rows):
        where = f"{memory_id} seq {r.get('seq')}"
        if r.get("seq") != i:
            return False, [f"{where}: seq not dense (expected {i})."]
        et = r.get("event_type")
        if et not in EVENT_TYPES:
            return False, [f"{where}: unknown event_type {et!r}."]
        if i == 0 and et != "STORED":
            return False, [f"{where}: chain does not begin with STORED."]
        if i > 0 and et == "STORED":
            return False, [f"{where}: STORED after birth — duplicated genesis semantics."]
        if r.get("prev_hash") != expected_prev:
            return False, [f"{where}: prev_hash does not link (chain broken or grafted)."]

        try:
            payload = json.loads(r["payload_json"]) if isinstance(r["payload_json"], str) else r["payload_json"]
        except Exception:
            return False, [f"{where}: payload_json is not valid JSON."]

        recomputed, _ = compute_entry_hash(
            memory_id=memory_id,
            seq=r["seq"],
            prev_hash=r["prev_hash"],
            event_type=et,
            actor_id=r["actor_id"],
            reason=r["reason"],
            created_at=r["created_at"],
            payload=payload,
        )
        if recomputed != r["entry_hash"]:
            return False, [f"{where}: entry_hash does not recompute — content tampered."]

        ts = _format_ts(r["created_at"])
        if prev_ts is not None and ts < prev_ts:
            return False, [
                f"{where}: created_at {ts} precedes the previous event's "
                f"{prev_ts} — a custody chain cannot run backwards in time."
            ]
        prev_ts = ts
        expected_prev = r["entry_hash"]

    return True, []


def verify_custody_chain(cur, memory_id: str) -> tuple[bool, list[str]]:
    """Load and verify one memory's full custody chain against live rows."""
    cur.execute(
        """
        SELECT memory_id, seq, prev_hash, entry_hash, event_type, actor_id,
               reason, payload_json, created_at
          FROM custody_chain
         WHERE memory_id = %s
         ORDER BY seq ASC
        """,
        (memory_id,),
    )
    cols = ["memory_id", "seq", "prev_hash", "entry_hash", "event_type",
            "actor_id", "reason", "payload_json", "created_at"]
    rows = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        # Defensive JSONB normalization — drivers disagree about shape
        # (dict, str, bytes). Same discipline as audit.chain.verify_chain;
        # MNEME never needs this since SQLite stores payload_json as TEXT.
        pj = d["payload_json"]
        if isinstance(pj, (bytes, bytearray)):
            pj = pj.decode("utf-8", errors="replace")
        d["payload_json"] = pj
        rows.append(d)
    return verify_custody_rows(memory_id, rows)
