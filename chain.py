"""
STIGMERGY — Per-node tamper-evident hash chain (Invariants 3 & 5).

Adapted from CRONOS's open/seal/verify pattern with one deliberate
inversion: CRONOS owned its transaction (single-process SQLite, the
tracer WAS the unit of work). Here the TRANSACTION BELONGS TO THE
CALLER. append_event() takes a live cursor and never commits, so the
state change and its audit event land in the same CockroachDB
transaction or not at all. If this module opened its own transaction,
Invariant 3 ("no committed state transition may exist without a
corresponding audit event") would be structurally false no matter how
careful the calling code was.

Hash formula (documented here, verified in verify_chain, tested in
tests/):

    entry_hash = sha256(
        prev_hash
        || canonical_json({
             "node_id":    node_id,
             "seq":        seq,
             "event_type": event_type,
             "reason":     reason,
             "created_at": <canonical timestamp string>,
             "payload":    payload,
           })
    )                                              -> hex digest

The timestamp is APP-GENERATED (UTC, microsecond precision) and inserted
explicitly rather than left to DEFAULT now(): the hash must cover it
(otherwise timestamp tampering is undetectable), and to cover it the
value must exist before the INSERT. One formatter (_format_ts) is used
both when writing and when verifying, so round-tripping through
TIMESTAMPTZ cannot introduce drift.

Genesis: prev_hash of seq 0 is sha256(b"STIGMERGY_GENESIS") — a
documented constant, never empty string or NULL, so a genesis row is
distinguishable from a broken chain by construction (see schema.sql).
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .canonical import canonical_json

GENESIS_PREV_HASH = hashlib.sha256(b"STIGMERGY_GENESIS").hexdigest()


def _format_ts(ts: datetime) -> str:
    """
    The single canonical timestamp formatter. UTC, microsecond precision,
    ISO 8601 with explicit offset. CockroachDB TIMESTAMPTZ stores
    microsecond-precision UTC, so a value written through this formatter
    and read back through psycopg re-formats to the identical string.
    """
    if ts.tzinfo is None:
        raise ValueError("Naive datetime in audit chain — timestamps must be timezone-aware UTC.")
    return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")


def compute_entry_hash(
    *,
    node_id: str,
    seq: int,
    prev_hash: str,
    event_type: str,
    reason: str,
    created_at: datetime,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """
    Returns (entry_hash, canonical_payload_str).

    The canonical payload string is returned so the caller stores the
    EXACT bytes that were hashed — payload_json in the row and the bytes
    under entry_hash can never diverge by construction.
    """
    canonical_payload = canonical_json(payload)
    envelope = canonical_json(
        {
            "node_id": node_id,
            "seq": seq,
            "event_type": event_type,
            "reason": reason,
            "created_at": _format_ts(created_at),
            # Nest the already-canonical payload as a parsed object so the
            # envelope is one canonical document, not string-in-string.
            "payload": json.loads(canonical_payload),
        }
    )
    entry_hash = hashlib.sha256((prev_hash + envelope).encode("utf-8")).hexdigest()
    return entry_hash, canonical_payload


@dataclass(frozen=True)
class AuditEntry:
    node_id: str
    seq: int
    prev_hash: str
    entry_hash: str
    event_type: str
    reason: str
    created_at: datetime


def append_event(
    cur,
    *,
    node_id: str,
    event_type: str,
    reason: str,
    payload: dict[str, Any],
) -> AuditEntry:
    """
    Append one event to node_id's chain, INSIDE THE CALLER'S TRANSACTION.

    Contract:
      - `cur` is a psycopg cursor on an open transaction. This function
        performs reads and one INSERT; it never commits, never rolls back.
      - The caller performs its state-changing writes in the same
        transaction. Commit is the caller's act; atomicity is the point.
      - `reason` must be non-empty — schema says NOT NULL, we say it
        earlier and with a better error message.

    Concurrency: the SELECT ... FOR UPDATE on the current head serializes
    concurrent appenders on the same node_id. Under CockroachDB's
    serializable isolation, contention surfaces as retryable 40001
    errors — the caller's transaction runner must retry (see
    run_in_transaction below). The UNIQUE(node_id, prev_hash) and
    PK(node_id, seq) constraints remain the last line of defense: if the
    locking logic ever regresses, a fork becomes a constraint violation,
    not a corrupted chain.
    """
    if not reason or not reason.strip():
        raise ValueError("Audit events require a non-empty reason (Invariant 3).")
    if not event_type or not event_type.strip():
        raise ValueError("Audit events require a non-empty event_type (Invariant 3).")

    cur.execute(
        """
        SELECT seq, entry_hash
          FROM audit_chain
         WHERE node_id = %s
         ORDER BY seq DESC
         LIMIT 1
           FOR UPDATE
        """,
        (node_id,),
    )
    head = cur.fetchone()
    if head is None:
        seq, prev_hash = 0, GENESIS_PREV_HASH
    else:
        seq, prev_hash = head[0] + 1, head[1]

    created_at = datetime.now(timezone.utc)
    entry_hash, canonical_payload = compute_entry_hash(
        node_id=node_id,
        seq=seq,
        prev_hash=prev_hash,
        event_type=event_type,
        reason=reason,
        created_at=created_at,
        payload=payload,
    )

    cur.execute(
        """
        INSERT INTO audit_chain
            (node_id, seq, prev_hash, entry_hash, event_type, reason,
             payload_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s::JSONB, %s)
        """,
        (node_id, seq, prev_hash, entry_hash, event_type, reason,
         canonical_payload, created_at),
    )

    return AuditEntry(
        node_id=node_id,
        seq=seq,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        event_type=event_type,
        reason=reason,
        created_at=created_at,
    )


@dataclass(frozen=True)
class ChainVerification:
    node_id: str
    ok: bool
    length: int
    first_broken_seq: int | None
    detail: str


def verify_chain(cur, node_id: str) -> ChainVerification:
    """
    Walk node_id's chain from genesis, recomputing every hash.

    Detects, and names explicitly:
      - wrong genesis prev_hash,
      - gaps in seq,
      - linkage breaks (prev_hash != previous entry_hash),
      - content tampering (recomputed entry_hash != stored entry_hash),
    Reports the FIRST broken seq — everything before it remains
    trustworthy, everything at and after it does not. Explicit
    degradation over hidden recovery (Failure philosophy).

    NOTE on re-canonicalization: payload_json was stored as canonical
    bytes, but JSONB is a parsed representation, so we canonicalize the
    parsed object again on the way out. canonical_json is a pure function
    of the parsed value, so this is exact — Decimals were stored as
    strings precisely so that this round trip cannot lose information.
    """
    cur.execute(
        """
        SELECT seq, prev_hash, entry_hash, event_type, reason,
               payload_json, created_at
          FROM audit_chain
         WHERE node_id = %s
         ORDER BY seq ASC
        """,
        (node_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return ChainVerification(node_id, True, 0, None, "empty chain (vacuously valid)")

    expected_prev = GENESIS_PREV_HASH
    for i, (seq, prev_hash, entry_hash, event_type, reason, payload, created_at) in enumerate(rows):
        # Drivers disagree about JSONB's Python shape (dict, str, bytes,
        # wrapper types). Normalize defensively; anything that is not a
        # dict after normalization is REPORTED as a broken chain, never
        # raised — corruption must be a finding, not a crash.
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return ChainVerification(
                    node_id, False, len(rows), seq,
                    "payload_json is not parseable JSON",
                )
        if not isinstance(payload, dict):
            return ChainVerification(
                node_id, False, len(rows), seq,
                f"payload_json is not a JSON object: got {type(payload).__name__}",
            )
        if seq != i:
            return ChainVerification(
                node_id, False, len(rows), seq,
                f"sequence gap: expected seq {i}, found {seq}",
            )
        if prev_hash != expected_prev:
            return ChainVerification(
                node_id, False, len(rows), seq,
                "linkage broken: prev_hash does not match previous entry_hash"
                + (" (bad genesis constant)" if seq == 0 else ""),
            )
        recomputed, _ = compute_entry_hash(
            node_id=node_id,
            seq=seq,
            prev_hash=prev_hash,
            event_type=event_type,
            reason=reason,
            created_at=created_at,
            payload=payload,
        )
        if recomputed != entry_hash:
            return ChainVerification(
                node_id, False, len(rows), seq,
                "content tampered: recomputed entry_hash differs from stored",
            )
        expected_prev = entry_hash

    return ChainVerification(node_id, True, len(rows), None, "chain verified end to end")


def run_in_transaction(conn, fn, *, max_retries: int = 5):
    """
    Execute fn(cur) in one transaction with CockroachDB retry discipline.

    fn receives a cursor, performs its state writes AND its append_event
    calls, and returns a value. On serialization failure (SQLSTATE 40001)
    the whole unit retries — which is correct precisely because state
    change and audit event are one unit. Any other exception rolls back
    and propagates: a failed audit write rejects the state transition
    (Failure philosophy, third clause).

    ONLY 40001 is retried, deliberately. 40003 (statement completion
    unknown) means "the commit MAY have happened" — blindly retrying an
    append whose commit succeeded would duplicate the audit event.
    Ambiguity must surface and be resolved by reading the chain, not be
    papered over with a retry. 40001 is the only SQLSTATE CockroachDB
    documents as requiring client-side retry.

    Retries use exponential backoff with jitter: simultaneous retryers
    without jitter re-collide in lockstep (thundering herd), which under
    contention degrades into starvation.
    """
    BASE_DELAY, MAX_DELAY = 0.01, 1.0
    for attempt in range(max_retries):
        try:
            with conn.cursor() as cur:
                result = fn(cur)
            conn.commit()
            return result
        except Exception as exc:
            conn.rollback()
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate == "40001" and attempt < max_retries - 1:
                time.sleep(min(BASE_DELAY * (2 ** attempt) + random.uniform(0, BASE_DELAY), MAX_DELAY))
                continue
            raise
    raise RuntimeError("unreachable")
