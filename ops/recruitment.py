"""
STIGMERGY — Recruitment signals: emission, weighted consensus, migration.

This is where coordination happens WITHOUT a coordinator. A region that
finds a memory resonant emits a recruitment signal into shared state;
other regions read those signals and, when weighted consensus crosses a
quorum, the memory migrates. No region commands another. No message bus.
Signals are written and observed — stigmergy, not messaging.

Four disciplines govern this module, each load-bearing:

1. DECAY IS READ, NEVER WRITTEN. Effective strength is
       signal_strength * exp(-decay_rate * elapsed_seconds)
   evaluated in SQL at resolution time over the immutable created_at.
   No UPDATE ever mutates signal_strength; no job decays it in the
   background. The stored columns are initial conditions. This is the
   one sanctioned appearance of exp()-over-floats in the system, and it
   is confined to a read-time biological heuristic (schema header).
   The SQL casts the whole expression to FLOAT8 EXPLICITLY: CockroachDB
   (unlike Postgres) rejects mixed DECIMAL/FLOAT arithmetic outright
   ("unsupported binary operator: <decimal> * <float>"), and the casts
   also make the float's confinement visible in the query text itself —
   the heuristic is float end to end, the verdict never is (REC-001).

2. FLOAT FILTERS, FRACTION DECIDES (Invariant 2). The decayed strength
   (float) only THRESHOLDS which signals are still alive. Among live
   signals, each one's VOTE is its vigor — an exact Fraction — and the
   verdict is a Fraction comparison against quorum. No float and no LLM
   sits in the final decision; a float that merely gates liveness is not
   in the decision path, a float in the comparison would be.

3. COOLDOWN IS A WHERE-CLAUSE, NOT AN IF (Invariant 6). The migration
   UPDATE guards last_migrated_at in SQL. A memory that migrated within
   the cooldown window matches zero rows and the migration is rejected
   atomically — no SELECT-then-check TOCTOU that two concurrent
   migrations could slip through. (The memory row IS selected first,
   but FOR UPDATE — it is locked, not merely read, so the subsequent
   guarded UPDATE remains the single arbiter; the read exists to
   capture from_region/old_tier for the audit payload and to
   distinguish "nonexistent" from "cooling down", REC-003/REC-004.)

4. RESOLUTION AND MIGRATION ARE ONE TRANSACTION. Accepting consensus,
   migrating the memory, marking the signal resolved, and sealing every
   audit event happen together or not at all (shared-cursor contract:
   this module never commits).

Consensus semantics (REC-002, ratified after the collective audit):
a signal is a region RECRUITING the memory TO its origin_region. When
resolving toward target_region, only live signals whose origin_region
equals the target VOTE FOR; every other live signal still counts in the
denominator. Quorum 1/2 therefore means "the target's live vigor is at
least half of everything achievable across the whole live field" —
a caller cannot cherry-pick a target and claim foreign vigor as
support, and competing regions' signals dilute instead of inflate.
Non-target live signals are left PENDING on success: they may drive
their own later resolution, subject to the cooldown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from audit.canonical import quantize
from audit.chain import append_event
from .authority import require_node_capability, require_region_capability

# A signal whose decayed strength falls below this is effectively gone —
# a late arrival can still see a faint pheromone, but it no longer counts
# toward liveness. Float, because it gates a float; never in a verdict.
LIVENESS_FLOOR = 0.05

# Weighted consensus quorum: live vigor in favor must reach this fraction
# of total live vigor for the memory to migrate. Exact Fraction — this is
# the decision threshold, and the decision path is rational-only.
CONSENSUS_QUORUM = Fraction(1, 2)

# Invariant 6: a memory may not migrate again within this window.
DEFAULT_COOLDOWN = timedelta(minutes=5)

VALID_REASONS = ("HIGH_RESONANCE", "NOVEL_PATTERN", "REINFORCEMENT", "CONFLICT")


class CooldownActive(RuntimeError):
    """The memory migrated within its cooldown window (Invariant 6)."""


class RegionUnavailable(RuntimeError):
    """
    target_region exists but is not ACTIVE (REC-011). The FK on
    memories.region_id can verify existence, but only the application
    can verify status — migrating into a RETIRED or reshaping region is
    semantically invalid even though it is referentially valid.
    """


class MemoryOrphaned(RuntimeError):
    """
    The memory is currently tier ORPHANED (REC-018). recall() is the
    sanctioned exit from ORPHANED — it seals a REDISCOVERED event
    precisely because "no longer forgotten" is a fact worth its own
    audit trail (ops/memories.py). Consensus migration used to skip
    that: it locks the memory row, sees old_tier == 'ORPHANED', and
    happily overwrote tier to 'REGIONAL' under a plain MEMORY_MIGRATED
    event, silently resurrecting a memory nobody explicitly rediscovered.
    Recruitment now refuses instead: recall() it first.
    """


@dataclass(frozen=True)
class EmittedSignal:
    signal_id: str
    memory_id: str
    origin_region: str
    vigor: Decimal
    expires_at: datetime


def emit_signal(
    cur,
    *,
    node_id: str,
    origin_region: str,
    memory_id: str,
    reason: str,
    vigor: Fraction,
    ttl: timedelta = timedelta(minutes=30),
    signal_strength: Fraction = Fraction(1),
    decay_rate: Fraction = Fraction(15, 100),
) -> EmittedSignal:
    """
    Emit one recruitment signal into shared state and seal SIGNAL_EMITTED.

    vigor is the signal's VOTE weight if it survives to a resolution —
    an exact Fraction in [0,1], quantized only at the SQL boundary.
    signal_strength and decay_rate are the initial conditions of the
    read-time decay curve; they are never rewritten after this insert.

    All validation happens BEFORE the cursor is touched — boundaries
    reject, they don't let garbage travel to a CHECK constraint that
    reports the violation in the database's words instead of ours.

    Idempotent per (memory_id, origin_region) while a signal is live: a
    region gets ONE live vote per memory (REDTEAM F-2 anti-flood). A
    second emission before the first resolves returns the existing signal
    unchanged and seals no new event — no transition occurred.
    """
    if reason not in VALID_REASONS:
        raise ValueError(f"reason must be one of {VALID_REASONS}, got {reason!r}.")
    for name, val in (("vigor", vigor), ("signal_strength", signal_strength),
                      ("decay_rate", decay_rate)):
        if not isinstance(val, Fraction):
            raise TypeError(f"{name} must be Fraction, got {type(val).__name__} — "
                            "no floats on the emission path.")
    if not (0 <= vigor <= 1):
        raise ValueError(f"vigor {vigor} outside [0,1].")
    if not (0 <= signal_strength <= 1):
        raise ValueError(f"signal_strength {signal_strength} outside [0,1].")
    if not (decay_rate > 0):
        raise ValueError(f"decay_rate {decay_rate} must be > 0.")
    # REC-007: a nonpositive ttl would otherwise surface as an opaque
    # CHECK(expires_at > created_at) violation deep in the driver.
    if not isinstance(ttl, timedelta):
        raise TypeError(f"ttl must be timedelta, got {type(ttl).__name__}.")
    if ttl <= timedelta(0):
        raise ValueError(f"ttl must be positive, got {ttl!r} — a signal that "
            "expires at or before its own creation is not a signal.")

    require_region_capability(
        cur, node_id=node_id, region_id=origin_region, capability="SIGNAL"
    )

    now = datetime.now(timezone.utc)
    expires_at = now + ttl
    q_vigor = quantize(vigor, "vigor")
    q_strength = quantize(signal_strength, "signal_strength")
    q_decay = quantize(decay_rate, "decay_rate")

    # REC-013: the FKs on memory_id and origin_region already protect the
    # schema, but their violation surfaces as the driver's IntegrityError,
    # in the database's words. Translate 23503 into OUR boundary error —
    # race-free (unlike a SELECT-then-INSERT pre-check) and zero extra
    # round trips. Same sqlstate-extraction pattern as chain.py's 23505.
    #
    # REDTEAM F-2 (anti-flood): the partial unique index
    # one_live_signal_per_region caps a region at ONE live (PENDING) signal
    # per memory, so the consensus denominator counts regions, not rows. A
    # duplicate emission is therefore a BENIGN no-op, not an error: ON
    # CONFLICT DO NOTHING makes it idempotent WITHOUT aborting the caller's
    # transaction (a raised 23505 would poison the whole agent round). The
    # existing live pheromone is returned unchanged, and — because no row
    # was inserted, i.e. no state transition — no SIGNAL_EMITTED event is
    # sealed (Invariant 3: an event marks a transition, and none occurred).
    try:
        cur.execute(
            """
            INSERT INTO recruitment_signals
                (origin_region, memory_id, reason, vigor,
                 signal_strength, decay_rate, status, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'PENDING', %s, %s)
            ON CONFLICT (memory_id, origin_region) WHERE status = 'PENDING'
            DO NOTHING
            RETURNING id
            """,
            (origin_region, memory_id, reason, q_vigor,
             q_strength, q_decay, now, expires_at),
        )
    except Exception as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate is None:
            diag = getattr(exc, "diag", None)
            sqlstate = getattr(diag, "sqlstate", None)
        if sqlstate == "23503":  # foreign_key_violation
            raise LookupError(
                f"emit_signal: memory {memory_id!r} or region "
                f"{origin_region!r} does not exist — the signal references "
                "a row that is not there."
            ) from exc
        raise

    inserted = cur.fetchone()
    if inserted is None:
        # A live pheromone from this region for this memory already exists.
        # Return it unchanged; emit is idempotent and no transition occurred.
        cur.execute(
            """
            SELECT id, vigor, expires_at
              FROM recruitment_signals
             WHERE memory_id = %s AND origin_region = %s AND status = 'PENDING'
            """,
            (memory_id, origin_region),
        )
        existing = cur.fetchone()
        if existing is None:
            # The conflict fired but the row is gone: a serialization anomaly,
            # not a state this code may paper over. Surface it (the caller's
            # run_in_transaction rolls back; retry reads a settled world).
            raise RuntimeError(
                "emit_signal: ON CONFLICT fired but no PENDING signal found "
                f"for memory {memory_id!r} / region {origin_region!r} — "
                "concurrent resolution race; retry the transaction."
            )
        eid, evigor, eexpires = existing
        return EmittedSignal(str(eid), str(memory_id), origin_region,
                             evigor, eexpires)

    signal_id = str(inserted[0])
    append_event(
        cur,
        node_id=node_id,
        event_type="SIGNAL_EMITTED",
        reason=reason,
        payload={
            "signal_id": signal_id,
            "memory_id": str(memory_id),
            "origin_region": origin_region,
            "vigor": q_vigor,
            "signal_strength": q_strength,
            "decay_rate": q_decay,
        },
    )
    return EmittedSignal(signal_id, str(memory_id), origin_region, q_vigor, expires_at)


@dataclass(frozen=True)
class LiveSignal:
    signal_id: str
    origin_region: str
    vigor: Fraction          # exact vote weight
    decayed_strength: float   # liveness gate only, never a vote


@dataclass(frozen=True)
class ConsensusOutcome:
    memory_id: str
    target_region: str
    live_signals: int
    votes_for: int
    vigor_for: Fraction
    vigor_max: Fraction       # REC-009: max achievable = Fraction(live_signals)
    quorum: Fraction
    reached: bool

    @property
    def ratio_exact(self) -> Fraction:
        return Fraction(0) if self.vigor_max == 0 else self.vigor_for / self.vigor_max


def _validate_quorum(quorum: Fraction) -> None:
    # REC-006: same discipline as reinforce_confidence's alpha (OPS-013).
    # A float quorum would contaminate the exact comparison; an
    # out-of-range one makes consensus vacuous (<= 0) or unreachable (> 1).
    if not isinstance(quorum, Fraction):
        raise TypeError(f"quorum must be Fraction, got {type(quorum).__name__} — "
                        "the decision path is rational-only.")
    if not (0 < quorum <= 1):
        raise ValueError(f"quorum {quorum} outside (0, 1].")


def _validate_live(live: list[LiveSignal]) -> None:
    # REC-006: a LiveSignal hand-built with a float vigor would turn
    # sum() and the comparison into float arithmetic — the exact OPS-013
    # contamination class, one dataclass field away. Reject at the
    # decision boundary, where it matters.
    #
    # REC-014: a duplicated signal_id would let one signal vote twice.
    # Impossible when the list comes from resolve_recruitment's single
    # SELECT (rows are keyed by PK), but this function is public and the
    # decision boundary does not get to assume its callers are careful —
    # and since resolve now routes through compute_consensus, this guard
    # is defense-in-depth against data corruption on the DB path too.
    seen: set[str] = set()
    for s in live:
        if s.signal_id in seen:
            raise ValueError(
                f"signal {s.signal_id!r} appears more than once — a signal "
                "cannot vote twice (REC-014)."
            )
        seen.add(s.signal_id)
        if not isinstance(s.vigor, Fraction):
            raise TypeError(
                f"signal {s.signal_id!r}: vigor must be Fraction, got "
                f"{type(s.vigor).__name__} — floats do not enter the decision path."
            )
        if not (0 <= s.vigor <= 1):
            raise ValueError(
                f"signal {s.signal_id!r}: vigor {s.vigor} outside [0,1] — the "
                "schema should have made this impossible; treat as corruption."
            )


def compute_consensus(
    live: list[LiveSignal],
    quorum: Fraction = CONSENSUS_QUORUM,
    *,
    target_region: str,
) -> bool:
    """
    THE decision function, in pure Fraction, testable without a database.
    resolve_recruitment calls this — not a private copy of its math — so
    the function the tests attack adversarially is byte-for-byte the one
    that decides in production (REC-010 follow-through).

    Consensus interpretation: votes FOR are the live signals recruiting
    toward target_region (origin_region == target_region); the
    denominator is the maximum achievable vigor across ALL live signals
    (one full unit each). quorum = 1/2 therefore means "at least half of
    everything achievable in the live field points HERE" — robust to
    signal count, and immune to cherry-picking a target (REC-002).

    target_region is REQUIRED (REC-010). The former target_region=None
    "everyone votes for" mode was a second semantics with no production
    caller — a decision function does not get to mean two things.

    NOTE: this takes already-filtered LIVE signals. The float→liveness
    gate happens in SQL/resolution; by the time vigor reaches this
    function, every float has done its job and left.
    """
    _validate_quorum(quorum)
    _validate_live(live)
    if not live:
        return False
    vigor_for = sum(
        (s.vigor for s in live if s.origin_region == target_region), Fraction(0)
    )
    return vigor_for >= quorum * Fraction(len(live))


def resolve_recruitment(
    cur,
    *,
    node_id: str,
    memory_id: str,
    target_region: str,
    quorum: Fraction = CONSENSUS_QUORUM,
    liveness_floor: float = LIVENESS_FLOOR,
    cooldown: timedelta = DEFAULT_COOLDOWN,
) -> ConsensusOutcome:
    """
    Resolve all PENDING signals for a memory: read them with decay
    computed in SQL, filter to live ones, compute Fraction consensus via
    compute_consensus (votes filtered by origin_region == target_region,
    REC-002 — one decision function, no private copy of its math),
    and — if reached — migrate the memory to target_region under the
    Invariant 6 cooldown guard. All in the caller's transaction.

    Only PENDING signals participate (REC-017). ACCEPTED, REJECTED and
    EXPIRED rows are history — Invariant 8 keeps them forever, but a
    resolved signal never votes again; resolution consumes it.

    Returns the ConsensusOutcome either way. If consensus is not reached,
    signals are left PENDING (they may yet accrue more votes, or decay to
    EXPIRED via the sweep) — the system preserves uncertainty rather than
    forcing a verdict (Failure philosophy).

    Raises:
      LookupError       — memory_id or target_region does not exist
                          (REC-004, REC-011).
      RegionUnavailable — target_region exists but is not ACTIVE: the FK
                          verifies existence, only this check verifies
                          status (REC-011).
      ValueError        — target_region is the memory's current region: a
                          no-op migration would burn the cooldown and seal
                          a MEMORY_MIGRATED event for nothing (REC-005).
      MemoryOrphaned    — the memory is tier ORPHANED; recall() it first
                          so rediscovery is itself audited (REC-018).
      CooldownActive    — consensus reached but Invariant 6 rejected the
                          migration; nothing changed.
    """
    _validate_quorum(quorum)
    if not (0 < liveness_floor < 1):
        raise ValueError(f"liveness_floor {liveness_floor} outside (0, 1).")

    require_region_capability(
        cur, node_id=node_id, region_id=target_region, capability="RESOLVE"
    )

    now = datetime.now(timezone.utc)

    # REC-011: existence AND status. The FK on memories.region_id would
    # reject a nonexistent target (as an opaque IntegrityError, mid-
    # UPDATE), but no constraint can reject a RETIRED or reshaping one.
    # FOR SHARE locks the row for the rest of THIS transaction: no
    # concurrent status change (region retirement, future split/merge CAS)
    # can slip in between this check and the migration UPDATE below.
    cur.execute(
        "SELECT status FROM memory_regions WHERE region_id = %s FOR SHARE",
        (target_region,),
    )
    region = cur.fetchone()
    if region is None:
        raise LookupError(f"target region {target_region!r} does not exist.")
    if region[0] != "ACTIVE":
        raise RegionUnavailable(
            f"target region {target_region!r} has status {region[0]!r} — "
            "only ACTIVE regions recruit. Nothing changed."
        )

    # REC-003/REC-004: lock the memory row and capture its CURRENT region
    # and tier — the audit payload must record where the memory migrated
    # FROM (oscillation forensics is the whole point of the cooldown),
    # and a nonexistent memory must be a LookupError, not a misdiagnosed
    # cooldown. FOR UPDATE means this read introduces no TOCTOU: the row
    # cannot change under us before the guarded UPDATE below.
    cur.execute(
        "SELECT region_id, tier FROM memories WHERE id = %s FOR UPDATE",
        (memory_id,),
    )
    mem = cur.fetchone()
    if mem is None:
        raise LookupError(f"memory {memory_id} does not exist.")
    from_region, old_tier = mem
    if from_region == target_region:
        raise ValueError(
            f"memory {memory_id} is already in region {target_region!r} — "
            "a same-region migration is a no-op that would burn the "
            "cooldown and seal a meaningless MEMORY_MIGRATED event."
        )
    # REC-018: ORPHANED is not just another prior tier. recall() is the
    # one sanctioned exit, and it seals REDISCOVERED for exactly that
    # reason. Letting consensus migrate an ORPHANED memory straight to
    # REGIONAL would resurrect it under a plain MEMORY_MIGRATED event,
    # bypassing the audit distinction the rest of the system relies on.
    if old_tier == "ORPHANED":
        raise MemoryOrphaned(
            f"memory {memory_id} is ORPHANED — recruitment consensus does "
            "not resurrect a memory. recall() it first (ORPHANED -> "
            "SHORT_TERM, REDISCOVERED, its own audited event); only then "
            "can it be recruited toward a new region."
        )

    # Decay computed HERE, in SQL, at read time, over immutable created_at.
    # REC-001: every operand is cast to FLOAT8 explicitly. CockroachDB has
    # no implicit DECIMAL/FLOAT coercion — signal_strength * <float>
    # raises "unsupported binary operator" (cockroach#108162). The casts
    # are also documentation: this expression is the sanctioned float
    # heuristic, float end to end, and it only populates decayed_strength
    # for the liveness filter below. It never becomes a vote.
    cur.execute(
        """
        SELECT id, origin_region, vigor,
               (signal_strength::FLOAT8) *
               exp((-decay_rate)::FLOAT8 *
                   extract(epoch from (%s - created_at))::FLOAT8)
                   AS decayed_strength
          FROM recruitment_signals
         WHERE memory_id = %s
           AND status = 'PENDING'
           AND expires_at > %s
           FOR UPDATE
        """,
        (now, memory_id, now),
    )
    rows = cur.fetchall()

    live: list[LiveSignal] = []
    for sig_id, origin, vigor_dec, decayed in rows:
        d = float(decayed)
        if d >= liveness_floor:
            live.append(LiveSignal(
                signal_id=str(sig_id),
                origin_region=origin,
                vigor=Fraction(vigor_dec),   # exact Decimal -> Fraction
                decayed_strength=d,
            ))

    votes = [s for s in live if s.origin_region == target_region]
    vigor_for = sum((s.vigor for s in votes), Fraction(0))
    vigor_max = Fraction(len(live)) if live else Fraction(0)
    # THE decision — the same adversarially-tested function, not a copy
    # of its formula. vigor_for above is reporting; this is the verdict.
    reached = compute_consensus(live, quorum, target_region=target_region)

    outcome = ConsensusOutcome(
        memory_id=str(memory_id),
        target_region=target_region,
        live_signals=len(live),
        votes_for=len(votes),
        vigor_for=vigor_for,
        vigor_max=vigor_max,
        quorum=quorum,
        reached=reached,
    )

    if not reached:
        # Preserve uncertainty. Signals stay PENDING; no state change, so
        # (correctly) no audit event — nothing transitioned.
        return outcome

    # --- consensus reached: migrate under cooldown guard --------------------
    # Invariant 6 as a WHERE-clause. If the memory migrated within the
    # cooldown window, this UPDATE matches zero rows: migration rejected
    # atomically. The row is already locked, so zero rows here can only
    # mean the cooldown guard fired — existence was settled above.
    cooldown_cutoff = now - cooldown
    cur.execute(
        """
        UPDATE memories
           SET region_id = %s,
               tier = 'REGIONAL',
               last_migrated_at = %s
         WHERE id = %s
           AND (last_migrated_at IS NULL OR last_migrated_at <= %s)
        RETURNING id
        """,
        (target_region, now, memory_id, cooldown_cutoff),
    )
    if cur.fetchone() is None:
        # The guard rejected it. Nothing changed; surface the rejection so
        # the caller preserves uncertainty instead of retrying blindly.
        raise CooldownActive(
            f"memory {memory_id} migrated within the last {cooldown}; "
            "Invariant 6 rejects a second migration. No state changed."
        )

    # Mark ONLY the voting signals ACCEPTED, resolved by the target region.
    # Live signals recruiting toward OTHER regions stay PENDING: they were
    # never accepted by anyone, and they may drive their own resolution
    # later (the cooldown they now face is the anti-oscillation brake).
    accepted_ids = [s.signal_id for s in votes]
    cur.execute(
        """
        UPDATE recruitment_signals
           SET status = 'ACCEPTED',
               resolved_at = %s,
               resolved_by_region = %s
         WHERE id = ANY(%s::UUID[])
        """,
        (now, target_region, accepted_ids),
    )

    append_event(
        cur,
        node_id=node_id,
        event_type="MEMORY_MIGRATED",
        reason=f"weighted recruitment consensus reached (quorum "
               f"{quorum.numerator}/{quorum.denominator})",
        payload={
            "memory_id": str(memory_id),
            "from_region": from_region,
            "target_region": target_region,
            "old_tier": old_tier,
            "new_tier": "REGIONAL",
            "live_signals": len(live),
            "votes_for": len(votes),
            "vigor_for": quantize(vigor_for, "vigor_for"),
            "vigor_max": quantize(vigor_max, "vigor_max"),
            "quorum": f"{quorum.numerator}/{quorum.denominator}",
            "accepted_signal_ids": accepted_ids,
        },
    )
    return outcome


def expire_signals(
    cur,
    *,
    node_id: str,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    """
    Sweep: mark up to `limit` PENDING signals past expires_at as EXPIRED.
    This is the cron-driven Lambda's job. EXPIRED signals get resolved_at
    (the sweep timestamp, for audit) but never resolved_by_region —
    nobody accepted them, they evaporated (schema state-coherence CHECK).

    REC-012: the sweep is BOUNDED now, not just documented. One call
    expires at most `limit` signals, so one audit event's payload holds
    at most `limit` ids — after a long cron outage the backlog drains as
    a series of bounded transactions instead of one giant event that
    could blow a row-size limit and take the audit write down with it.
    The subquery orders by expires_at ASC, which both drains oldest-first
    and matches INDEX (status, expires_at) exactly.

    The caller loops:  while run_in_transaction(conn, sweep) > 0: ...

    Returns the count expired in THIS call. Emits ONE SIGNALS_EXPIRED
    event per call with the swept ids — the transition is per-signal but
    the chunk is one logical act, and the payload names every id, so
    nothing is unaudited.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit must be int, got {type(limit).__name__}.")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}.")

    require_node_capability(cur, node_id=node_id, capability="MAINTAIN")

    now = now or datetime.now(timezone.utc)
    cur.execute(
        """
        UPDATE recruitment_signals
           SET status = 'EXPIRED', resolved_at = %s
         WHERE id IN (
               SELECT id
                 FROM recruitment_signals
                WHERE status = 'PENDING' AND expires_at <= %s
                ORDER BY expires_at ASC
                LIMIT %s
         )
        RETURNING id
        """,
        (now, now, limit),
    )
    expired_ids = [str(r[0]) for r in cur.fetchall()]
    if not expired_ids:
        return 0

    append_event(
        cur,
        node_id=node_id,
        event_type="SIGNALS_EXPIRED",
        reason="ttl elapsed; pheromones evaporated",
        payload={"expired_signal_ids": expired_ids, "count": len(expired_ids)},
    )
    return len(expired_ids)
