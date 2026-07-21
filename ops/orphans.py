"""
STIGMERGY — Orphan sweep: stale memories move to ORPHANED. Never deleted.

This is the cron-driven Lambda's second job (the first is expire_signals;
ARCHITECTURE.md deliberately gives the two Lambdas different patterns).
Invariant 8's flip side lives here: a memory that stops being recruited,
recalled, or reinforced does not disappear — its tier becomes ORPHANED,
a state from which recall() can resurrect it at any time (REDISCOVERED,
an audited event). Orphaning is a tier transition, so Invariant 3 applies
with full force: every swept memory lands in the audit chain, by id, with
the tier it held before.

Design decisions (each deliberate, each attackable in the next audit):

TIERS — SHORT_TERM and REGIONAL sweep; GLOBAL does not. Consolidation is
earned, and demoting a GLOBAL memory should be its own audited policy
decision in a future consolidation module — not a background job's side
effect. SWEEP_TIERS is a module constant, not a parameter: making it a
parameter would invite violating the decision casually, and changing it
should look like what it is (an architecture decision).

STATES — NEUTRAL and FORGOTTEN sweep by default; REINFORCED does not.
The axes are independent (schema), so a cold REINFORCED memory COULD
coherently orphan — but reinforcement is affirmative evidence of value,
and demoting it via cron violates least surprise. eligible_states is a
parameter so the policy is explicit and revisable; the default is the
conservative one. (A future confidence-decay module moving stale
REINFORCED -> NEUTRAL would feed this sweep without ever letting it
touch reinforced memories directly.)

STALENESS — max of ALL activity timestamps, not just recalls:
    greatest(created_at, last_accessed_at, last_reinforced_at,
             last_migrated_at) <= now - window
A memory reinforced yesterday but never recalled is NOT stale. A memory
rediscovered five minutes ago has a fresh last_accessed_at and is
protected for a full window — rediscovery self-protects, no extra
hysteresis mechanism needed. Each nullable column is COALESCEd to
created_at before greatest(): Postgres and CockroachDB happen to ignore
NULLs in greatest() (deviating from the SQL standard), but leaning on
that quirk is exactly the class of cross-platform subtlety this project
refuses to inherit silently. COALESCE makes the expression NULL-proof by
construction and costs nothing.

LOCKED READ, THEN WRITE — the sweep SELECTs candidates FOR UPDATE, then
UPDATEs by id. This is the REC-003 pattern, not a TOCTOU: the lock means
eligibility cannot change between the read and the write, and the read
exists because the audit payload must record from_tier — which UPDATE ...
RETURNING cannot provide (it returns new values). The WHERE-guard-only
pattern (Invariant 6 cooldown) is for writes that deliberately do NOT
lock first; this one locks.

BOUNDED CHUNKS — the REC-012 pattern: at most `limit` memories per call,
oldest activity first, one MEMORIES_ORPHANED event per chunk naming every
id. The caller loops:  while run_in_transaction(conn, sweep): ...
(an empty result is falsy). After a long cron outage the backlog drains
as bounded transactions, never one giant event.

Shared-cursor contract, stated once more: every function here takes a
live cursor and NEVER commits. The caller owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from audit.chain import _format_ts, append_event
from .authority import require_node_capability

# Tiers that sweep. GLOBAL is deliberately absent — see module docstring.
# ORPHANED is absent because orphaning an orphan is not a transition.
SWEEP_TIERS = ("SHORT_TERM", "REGIONAL")

# The schema's state axis, single source for validation here.
VALID_STATES = ("REINFORCED", "NEUTRAL", "FORGOTTEN")

# Conservative default: reinforcement protects. See module docstring.
DEFAULT_ELIGIBLE_STATES = ("NEUTRAL", "FORGOTTEN")

# A week of total silence before a memory orphans. Tunable per call;
# the demo will use minutes.
DEFAULT_STALENESS_WINDOW = timedelta(days=7)

DEFAULT_SWEEP_LIMIT = 1000


@dataclass(frozen=True)
class OrphanedMemory:
    memory_id: str
    from_tier: str


def _validate_window(window: timedelta) -> None:
    if not isinstance(window, timedelta):
        raise TypeError(f"window must be timedelta, got {type(window).__name__}.")
    if window <= timedelta(0):
        raise ValueError(f"window must be positive, got {window!r} — a "
                         "non-positive staleness window would orphan the present.")


def _validate_limit(limit: int) -> None:
    # Same discipline as expire_signals (REC-012): bool is an int subclass
    # by historical accident, reject it explicitly.
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit must be int, got {type(limit).__name__}.")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}.")


def _validate_states(eligible_states) -> tuple[str, ...]:
    """
    Normalize and validate the state filter. Returns a tuple in the
    caller's order. Empty, unknown, duplicated, or non-str entries are
    boundary errors — a sweep over zero states is a no-op someone
    believed was a sweep, and a typo'd state would silently match nothing.
    """
    if isinstance(eligible_states, str):
        raise TypeError("eligible_states must be a sequence of states, not a "
                        "bare string — ('NEUTRAL',) not 'NEUTRAL'.")
    states = tuple(eligible_states)
    if not states:
        raise ValueError("eligible_states cannot be empty — a sweep over zero "
                         "states silently does nothing.")
    seen: set[str] = set()
    for s in states:
        if not isinstance(s, str):
            raise TypeError(f"eligible_states entries must be str, got "
                            f"{type(s).__name__}.")
        if s not in VALID_STATES:
            raise ValueError(f"unknown state {s!r} — schema states are "
                             f"{VALID_STATES}. A typo here would silently "
                             "match nothing.")
        if s in seen:
            raise ValueError(f"state {s!r} appears more than once.")
        seen.add(s)
    return states


def _window_microseconds(window: timedelta) -> int:
    """
    Exact integer microseconds for the audit payload. total_seconds()
    returns a float, and floats do not enter audit payloads (canonical.py
    rejects them); this is the exact integer decomposition instead.
    """
    return (window.days * 86_400_000_000
            + window.seconds * 1_000_000
            + window.microseconds)


def sweep_orphans(
    cur,
    *,
    node_id: str,
    window: timedelta = DEFAULT_STALENESS_WINDOW,
    limit: int = DEFAULT_SWEEP_LIMIT,
    eligible_states = DEFAULT_ELIGIBLE_STATES,
    now: datetime | None = None,
) -> list[OrphanedMemory]:
    """
    Move up to `limit` stale memories to tier ORPHANED and seal ONE
    MEMORIES_ORPHANED event naming every id and the tier it held.

    Stale means: no activity of ANY kind (creation, recall,
    reinforcement, migration) within `window`. Stalest first.

    Returns the list of orphaned memories (empty list — falsy — when
    nothing qualified, so the cron loop reads naturally). state is never
    touched: orphaning is a tier transition; what the evidence says about
    the memory is a separate axis and remains whatever it was.
    """
    _validate_window(window)
    _validate_limit(limit)
    states = _validate_states(eligible_states)
    require_node_capability(cur, node_id=node_id, capability="MAINTAIN")
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware UTC — naive datetimes "
                         "do not enter the audit chain.")
    now = now or datetime.now(timezone.utc)
    cutoff = now - window

    # Locked read: candidates plus the from_tier the payload needs.
    # ORDER BY the same staleness expression — oldest silence drains
    # first. This expression has no covering index; the sweep is a cron
    # job over the non-orphaned working set, and that cost is accepted
    # and documented rather than half-fixed with a wrong index.
    cur.execute(
        f"""
        SELECT id, tier,
               greatest(
                   created_at,
                   coalesce(last_accessed_at,   created_at),
                   coalesce(last_reinforced_at, created_at),
                   coalesce(last_migrated_at,   created_at)
               ) AS last_activity
          FROM memories
         WHERE tier IN ({",".join("%s" for _ in SWEEP_TIERS)})
           AND state = ANY(%s::STRING[])
           AND greatest(
                   created_at,
                   coalesce(last_accessed_at,   created_at),
                   coalesce(last_reinforced_at, created_at),
                   coalesce(last_migrated_at,   created_at)
               ) <= %s
         ORDER BY last_activity ASC
         LIMIT %s
           FOR UPDATE
        """,
        (*SWEEP_TIERS, list(states), cutoff, limit),
    )
    rows = cur.fetchall()
    if not rows:
        return []

    orphaned = [OrphanedMemory(memory_id=str(r[0]), from_tier=r[1]) for r in rows]
    ids = [o.memory_id for o in orphaned]

    # The rows are locked; eligibility cannot have changed under us.
    cur.execute(
        """
        UPDATE memories
           SET tier = 'ORPHANED'
         WHERE id = ANY(%s::UUID[])
        """,
        (ids,),
    )

    append_event(
        cur,
        node_id=node_id,
        event_type="MEMORIES_ORPHANED",
        reason="no activity within the staleness window; "
               "tier -> ORPHANED (Invariant 8: a state, not a grave)",
        payload={
            "window_microseconds": _window_microseconds(window),
            "cutoff": _format_ts(cutoff),
            "eligible_states": list(states),
            "orphaned": [
                {"memory_id": o.memory_id, "from_tier": o.from_tier}
                for o in orphaned
            ],
            "count": len(orphaned),
        },
    )
    return orphaned
