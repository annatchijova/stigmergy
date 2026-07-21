"""
STIGMERGY — Core memory operations: store, recall, reinforce.

This module composes the three layers beneath it and adds no new
authority of its own:

  - embeddings.*   produces vectors (and declares, honestly, whether
                    they mean anything: is_semantic).
  - audit.chain    seals every tier/state/region transition into the
                    per-node hash chain, inside the SAME transaction.
  - schema.sql     makes invalid states unrepresentable.

Transactional contract — identical to audit.chain and stated once more
because everything depends on it: EVERY function here takes a live
cursor and NEVER commits. The caller owns the transaction (use
audit.chain.run_in_transaction). That is what makes Invariant 3 true by
construction rather than by good intentions.

Arithmetic discipline: confidence lives in Fraction while computed,
becomes Decimal (canonical scale, explicit rounding) only at the SQL and
audit-payload boundary, and is NEVER a float at any point in between.
psycopg returns DECIMAL columns as decimal.Decimal; Fraction(Decimal) is
an exact conversion, so the round trip loses nothing.

Auditing policy (deliberate, documented):
  - store()      -> MEMORY_STORED        (a row comes into existence)
  - reinforce()  -> MEMORY_REINFORCED    (state and confidence change)
  - recall()     -> no event for ordinary access. recall_count and
                    last_accessed_at are not tier/state/region, and
                    Invariant 3's letter covers exactly those three.
                    Auditing every read would flood the chain with noise
                    and dilute the events that matter.
  - recall() of an ORPHANED memory -> REDISCOVERED, because rediscovery
                    changes tier (ORPHANED -> SHORT_TERM), and Invariant 8
                    names it an audited event explicitly.

Provider integrity (closing the mixing-vectors P1 from the embeddings
audit): every MEMORY_STORED payload records provider_id, and
verify_provider() refuses to operate against a database populated by a
different provider. The audit chain is already the source of truth about
what happened; we read it instead of inventing a config table.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

from audit.canonical import quantize
from audit.chain import append_event
from .authority import require_active_node, require_region_capability

# Reinforcement step: c' = c + (1 - c) * ALPHA. Exact rational constant —
# deterministic, monotone, bounded in [0,1), fixed point at exactly 1.
#
# Saturation note: the Fraction never reaches 1, but the STORED value
# does — quantization at scale 10 rounds to 1.0000000000 once
# (1 - c) <= 5e-11. From c0 = 1/2 that happens at step n = 104
# (closed form: n >= log(1e-10)/log(1 - alpha) ~ 103.2; verified
# empirically and pinned by test). From then on the stored confidence
# is exactly 1 and reinforcement is a fixed point. This is a documented
# property, not an accident.
REINFORCEMENT_ALPHA = Fraction(1, 5)

DEFAULT_CONFIDENCE = Fraction(1, 2)  # mirrors schema DEFAULT 0.5


class ProviderMismatch(RuntimeError):
    """The database was populated by a different embedding provider."""


def _require_provider(provider) -> None:
    """
    Fail at the boundary with a clear error, not three calls deep with an
    AttributeError. Note: a malformed provider could NOT have committed
    unaudited state anyway — the exception aborts the caller's
    transaction whole — but our own discipline is that boundaries reject,
    they don't let garbage travel.
    """
    for attr in ("embed", "provider_id", "is_semantic"):
        if not hasattr(provider, attr):
            raise TypeError(
                f"provider lacks {attr!r} — it does not implement the "
                "EmbeddingProvider protocol (embeddings.base)."
            )


def reinforce_confidence(c: Fraction, alpha: Fraction = REINFORCEMENT_ALPHA) -> Fraction:
    """
    Pure reinforcement rule. Properties (each pinned by a test):
      - result stays in [0, 1] for input in [0, 1];
      - strictly increasing for c < 1; fixed point exactly at 1;
      - exact: Fraction in, Fraction out, no rounding anywhere.
    """
    if not isinstance(c, Fraction):
        raise TypeError(f"confidence must be Fraction, got {type(c).__name__} — "
                        "floats do not enter the decision path.")
    if not isinstance(alpha, Fraction):
        # A float alpha would pass the range check below and silently
        # contaminate the arithmetic (Fraction * float -> float), returning
        # a float from the function whose whole point is exactness. Found
        # by the collective's audit (OPS-013, underrated as test coverage).
        raise TypeError(f"alpha must be Fraction, got {type(alpha).__name__}.")
    if not (0 <= c <= 1):
        raise ValueError(f"confidence {c} outside [0,1] — the schema should have "
                         "made this impossible; treat as data corruption.")
    if not (0 < alpha < 1):
        raise ValueError(f"alpha {alpha} outside (0,1).")
    return c + (1 - c) * alpha


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def verify_provider(cur, provider) -> None:
    """
    Refuse to operate against a database populated by a different
    embedding provider. Cosine distances between vectors from different
    providers are perfectly valid numbers and perfectly meaningless —
    manufactured certainty, the exact thing the Failure philosophy
    forbids. Call this once at startup, before any store/recall.

    A virgin database (no MEMORY_STORED events) accepts any provider.
    A populated one accepts exactly the provider that populated it.
    """
    cur.execute(
        """
        SELECT DISTINCT payload_json->>'provider_id'
          FROM audit_chain
         WHERE event_type = 'MEMORY_STORED'
        """
    )
    seen = {row[0] for row in cur.fetchall()}
    seen.discard(None)  # pre-provider_id events would be a migration bug; surface below
    if not seen:
        return
    if seen != {provider.provider_id}:
        raise ProviderMismatch(
            f"Database contains vectors from provider(s) {sorted(seen)}, "
            f"but the active provider is {provider.provider_id!r}. "
            "Distances across providers are meaningless. Either switch back, "
            "or treat this as the migration event it is (see schema header: "
            "'Changing models is a migration event, not a config flip')."
        )


@dataclass(frozen=True)
class StoredMemory:
    memory_id: str
    region_id: str
    confidence: Decimal
    content_sha256: str


def store(
    cur,
    *,
    provider,
    node_id: str,
    region_id: str,
    content: str,
    reason: str,
    confidence: Fraction = DEFAULT_CONFIDENCE,
) -> StoredMemory:
    """
    Insert one memory and seal MEMORY_STORED into node_id's chain, in the
    caller's transaction. The audit payload carries the content's sha256
    (integrity without duplicating the content into the chain), the
    quantized confidence, and — non-negotiably — provider_id.
    """
    _require_provider(provider)
    if not content or not content.strip():
        raise ValueError("Refusing to store an empty memory — an episodic "
                         "memory without content is not a memory.")
    require_region_capability(
        cur, node_id=node_id, region_id=region_id, capability="STORE"
    )
    vector = provider.embed(content)

    # Local import: to_pgvector_literal lives with the providers, and this
    # module must not force an import order on the embeddings package.
    from embeddings.base import to_pgvector_literal

    conf = quantize(confidence, "confidence")
    cur.execute(
        """
        INSERT INTO memories (region_id, embedding, content, confidence)
        VALUES (%s, %s::VECTOR, %s, %s)
        RETURNING id
        """,
        (region_id, to_pgvector_literal(vector), content, conf),
    )
    memory_id = str(cur.fetchone()[0])
    digest = _content_sha256(content)

    append_event(
        cur,
        node_id=node_id,
        event_type="MEMORY_STORED",
        reason=reason,
        payload={
            "memory_id": memory_id,
            "region_id": region_id,
            "provider_id": provider.provider_id,
            "content_sha256": digest,
            "confidence": conf,
        },
    )
    return StoredMemory(memory_id, region_id, conf, digest)


@dataclass(frozen=True)
class RecallHit:
    memory_id: str
    content: str
    tier: str
    state: str
    confidence: Decimal
    distance: Decimal
    rediscovered: bool


@dataclass(frozen=True)
class RecallResult:
    hits: list[RecallHit]
    is_semantic: bool  # copied from the provider — see class docstring below
    region_id: str | None

    """
    is_semantic travels WITH the result, not as ambient knowledge the
    caller is supposed to remember. Any code that ranks, filters, or
    narrates relevance must check it; drawing relevance conclusions from
    a non-semantic provider's distances is fabricated certainty.
    """


def recall(
    cur,
    *,
    provider,
    node_id: str,
    query_text: str,
    region_id: str | None,
    limit: int = 5,
) -> RecallResult:
    """
    Vector search + access-metadata update + rediscovery handling.

    region_id set   -> DWELLING: prefix-constrained search, cheap by
                       construction (vector index prefix on region_id).
    region_id None  -> ROAMING: cross-region search, expensive by
                       construction. The cost asymmetry is the thesis.

    Distance operator: vectors are unit-normalized by both providers, so
    L2 (<->) ordering is monotonically equivalent to cosine ordering —
    and <-> is what the vector index accelerates. If a provider ever
    stops normalizing, validate_embedding's unit-norm guard (embeddings
    audit, pending) is the tripwire; this comment is the paper trail.

    Ordinary access is NOT audited (see module docstring). Rediscovery
    of an ORPHANED memory IS: tier -> SHORT_TERM + REDISCOVERED event,
    per Invariants 3 and 8. There is no volume exemption: if a recall
    surfaces N orphans, N tier transitions occurred and N events are
    sealed. "Too many audit events" is not a concept this system
    recognizes.

    Cross-provider rediscovery cannot occur under the startup contract:
    verify_provider() guarantees a single-provider database, so any
    ORPHANED memory this recall touches was stored by the same provider
    now rediscovering it.

    The reported `distance` is diagnostic output, not decision input.
    CockroachDB computes it as a float server-side; we surface it as-is
    (Decimal(str(f)) is an exact round-trip in Python 3 — shortest-repr).
    Any future module that THRESHOLDS on distance must quantize it
    explicitly at its own boundary or it does not enter a decision.
    """
    _require_provider(provider)
    if limit < 1:
        raise ValueError("limit must be >= 1")
    # Recall updates access metadata and can rediscover an ORPHANED memory;
    # it is therefore a mutation boundary even when it returns no hits.
    require_active_node(cur, node_id=node_id)
    vector = provider.embed(query_text)
    from embeddings.base import to_pgvector_literal
    literal = to_pgvector_literal(vector)

    if region_id is not None:
        cur.execute(
            """
            SELECT id, content, tier, state, confidence,
                   embedding <-> %s::VECTOR AS distance
              FROM memories
             WHERE region_id = %s
             ORDER BY distance
             LIMIT %s
            """,
            (literal, region_id, limit),
        )
    else:
        cur.execute(
            """
            SELECT id, content, tier, state, confidence,
                   embedding <-> %s::VECTOR AS distance
              FROM memories
             ORDER BY distance
             LIMIT %s
            """,
            (literal, limit),
        )
    rows = cur.fetchall()

    hits: list[RecallHit] = []
    now = datetime.now(timezone.utc)

    # Batch the common case: one UPDATE for every non-orphaned hit
    # (collective audit OPS-005). Rediscoveries stay per-row below —
    # each one carries its own audit event, and that per-event cost is
    # the point, not overhead to optimize away.
    ordinary_ids = [str(r[0]) for r in rows if r[2] != "ORPHANED"]
    if ordinary_ids:
        cur.execute(
            """
            UPDATE memories
               SET recall_count = recall_count + 1,
                   last_accessed_at = %s
             WHERE id = ANY(%s::UUID[])
            """,
            (now, ordinary_ids),
        )

    for mid, content, tier, state, confidence, distance in rows:
        mid = str(mid)
        rediscovered = tier == "ORPHANED"
        if rediscovered:
            # Tier change -> audited, same transaction (Invariants 3 & 8).
            cur.execute(
                """
                UPDATE memories
                   SET tier = 'SHORT_TERM',
                       recall_count = recall_count + 1,
                       last_accessed_at = %s
                 WHERE id = %s
                """,
                (now, mid),
            )
            append_event(
                cur,
                node_id=node_id,
                event_type="REDISCOVERED",
                reason="orphaned memory surfaced by recall; tier ORPHANED -> SHORT_TERM",
                payload={
                    "memory_id": mid,
                    "provider_id": provider.provider_id,
                    "previous_tier": "ORPHANED",
                    "new_tier": "SHORT_TERM",
                },
            )
        hits.append(RecallHit(
            memory_id=mid,
            content=content,
            tier="SHORT_TERM" if rediscovered else tier,
            state=state,
            confidence=confidence,
            distance=distance if isinstance(distance, Decimal) else Decimal(str(distance)),
            rediscovered=rediscovered,
        ))

    return RecallResult(hits=hits, is_semantic=provider.is_semantic, region_id=region_id)


@dataclass(frozen=True)
class ReinforcedMemory:
    memory_id: str
    old_confidence: Decimal
    new_confidence: Decimal
    state: str


def reinforce(cur, *, node_id: str, memory_id: str, reason: str) -> ReinforcedMemory:
    """
    Apply one reinforcement step: confidence moves toward 1 by the exact
    rational rule, state becomes REINFORCED. State change -> audited, same
    transaction. FOR UPDATE prevents two concurrent reinforcements from
    reading the same old confidence and losing one step (the read and the
    write are one locked unit).

    A FORGOTTEN memory can be reinforced — forgetting is a state, not a
    grave (Invariant 8), and reinforcement is precisely the evidence that
    it should not have been forgotten. The transition is visible in the
    audit payload (old_state) rather than blocked.

    Reinforcement is NOT migration. Invariant 6's cooldown governs
    region changes driven by recruitment consensus; reinforcing changes
    state and confidence, never region, so last_migrated_at plays no
    role here. Stated so the question does not recur.
    """
    cur.execute(
        """
        SELECT confidence, state, region_id
          FROM memories
         WHERE id = %s
           FOR UPDATE
        """,
        (memory_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"memory {memory_id} does not exist.")
    old_conf_decimal, old_state, region_id = row
    require_region_capability(
        cur, node_id=node_id, region_id=region_id, capability="REINFORCE"
    )

    old_conf = Fraction(old_conf_decimal)          # exact: Decimal -> Fraction
    new_conf = reinforce_confidence(old_conf)      # exact: pure Fraction rule
    new_conf_decimal = quantize(new_conf, "confidence")  # boundary: quantize once

    now = datetime.now(timezone.utc)
    cur.execute(
        """
        UPDATE memories
           SET confidence = %s,
               state = 'REINFORCED',
               last_reinforced_at = %s
         WHERE id = %s
        """,
        (new_conf_decimal, now, memory_id),
    )
    append_event(
        cur,
        node_id=node_id,
        event_type="MEMORY_REINFORCED",
        reason=reason,
        payload={
            "memory_id": str(memory_id),
            "old_state": old_state,
            "new_state": "REINFORCED",
            "old_confidence": quantize(old_conf, "old_confidence"),
            "new_confidence": new_conf_decimal,
            "alpha": f"{REINFORCEMENT_ALPHA.numerator}/{REINFORCEMENT_ALPHA.denominator}",
        },
    )
    return ReinforcedMemory(
        memory_id=str(memory_id),
        old_confidence=quantize(old_conf, "old_confidence"),
        new_confidence=new_conf_decimal,
        state="REINFORCED",
    )
