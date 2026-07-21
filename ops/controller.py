"""
STIGMERGY — Roaming/dwelling controller (agent_search_state).

The behavioral half of the cost asymmetry thesis: DWELLING searches one
region (vector index prefix — cheap by construction), ROAMING searches
everywhere (expensive by construction). This module decides WHICH,
per agent, from shared state only — the controller's inputs come from
what the agent reads out of `memories` (resonance density of recall
results), never from agent-local confidence (Invariant 1, and the
ARCHITECTURE.md table's own words).

The decision function is pure Fraction hysteresis:

    ema' = beta * observation + (1 - beta) * ema

    ROAMING  -> DWELLING   when ema' >= ENTER threshold
    DWELLING -> ROAMING    when ema' <= EXIT threshold
    DWELLING -> ROAMING    when stagnation_count reaches its limit
                           (dwelling stopped paying: ema' no longer
                            improves by at least MIN_IMPROVEMENT)

EXIT < ENTER, STRICTLY — the open band between them is where NEITHER
mode transitions, and that band IS the hysteresis. Equal thresholds
would be zero hysteresis: oscillation at a single boundary, the exact
failure the mechanism exists to prevent. The validator rejects
degenerate bands and a test pins it.

Threshold boundaries are INCLUSIVE on both sides (>= enter, <= exit).
Since exit < enter strictly, no single ema value can satisfy both — the
inclusivity is unambiguous by construction, not by luck.

AUDIT POLICY (extends Invariant 3's spirit to a table its letter does
not name — flagged for promotion into ARCHITECTURE.md): mode
TRANSITIONS are audited (AGENT_MODE_CHANGED, with the trigger named);
continuous EMA updates are not. This mirrors recall()'s policy exactly:
recall_count and last_accessed_at are bookkeeping, tier changes are
events. The EMA is bookkeeping; the mode flip is a decision.

WHAT DENSITY MEANS: resonance_density() is the fraction of recalled
confidence that comes from REINFORCED memories — NEUTRAL and FORGOTTEN
hits count in the denominator and contribute nothing. A field of
forgotten memories is not resonant, however dense. Note that density
consumes state and confidence — provider-independent FACTS produced by
exact arithmetic and audited decisions — never distance. Which memories
made it into the hit set does depend on the provider; RecallResult
carries is_semantic for callers narrating relevance, but switching
search mode is behavior, not a relevance claim.

Shared-cursor contract, as everywhere: no function here commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction

from audit.canonical import quantize
from audit.chain import append_event
from .authority import require_active_node

MODES = ("ROAMING", "DWELLING")

DEFAULT_BETA = Fraction(1, 4)
DEFAULT_ENTER = Fraction(3, 5)
DEFAULT_EXIT = Fraction(2, 5)
DEFAULT_STAGNATION_LIMIT = 5
DEFAULT_MIN_IMPROVEMENT = Fraction(1, 100)


class StateWriteConflict(Exception):
    """
    Two concurrent first-observations raced to create the same agent's
    row (both read absence, both inserted). Expected under serializable
    isolation, not corruption — the caller must retry the ENTIRE
    transaction, exactly like ChainWriteConflict in audit.chain.
    """


def _require_fraction(value, name: str) -> None:
    if not isinstance(value, Fraction):
        raise TypeError(f"{name} must be Fraction, got {type(value).__name__} — "
                        "floats do not enter the decision path.")


@dataclass(frozen=True)
class ControllerParams:
    """
    Hysteresis parameters. Validated eagerly at construction so an
    invalid parameterization cannot exist as an object, let alone reach
    a decision.
    """
    beta: Fraction = DEFAULT_BETA
    enter: Fraction = DEFAULT_ENTER
    exit: Fraction = DEFAULT_EXIT
    stagnation_limit: int = DEFAULT_STAGNATION_LIMIT
    min_improvement: Fraction = DEFAULT_MIN_IMPROVEMENT

    def __post_init__(self) -> None:
        _require_fraction(self.beta, "beta")
        _require_fraction(self.enter, "enter")
        _require_fraction(self.exit, "exit")
        _require_fraction(self.min_improvement, "min_improvement")
        if not (0 < self.beta < 1):
            raise ValueError(f"beta {self.beta} outside (0,1).")
        if not (0 <= self.exit <= 1) or not (0 <= self.enter <= 1):
            raise ValueError("thresholds must lie in [0,1].")
        if not (self.exit < self.enter):
            raise ValueError(
                f"exit ({self.exit}) must be STRICTLY below enter "
                f"({self.enter}) — the open band between them IS the "
                "hysteresis; equal thresholds are oscillation waiting."
            )
        if isinstance(self.stagnation_limit, bool) or not isinstance(self.stagnation_limit, int):
            raise TypeError("stagnation_limit must be int.")
        if self.stagnation_limit < 1:
            raise ValueError(f"stagnation_limit must be >= 1, got {self.stagnation_limit}.")
        if self.min_improvement < 0:
            raise ValueError(f"min_improvement {self.min_improvement} must be >= 0.")


DEFAULT_PARAMS = ControllerParams()


@dataclass(frozen=True)
class ControllerState:
    mode: str
    ema: Fraction
    ema_prev: Fraction
    stagnation_count: int


INITIAL_STATE = ControllerState(
    mode="ROAMING", ema=Fraction(0), ema_prev=Fraction(0), stagnation_count=0,
)


@dataclass(frozen=True)
class Decision:
    state: ControllerState          # the NEW state
    transitioned: bool
    trigger: str | None             # named cause, None when no transition


def resonance_density(pairs) -> Fraction:
    """
    Pure observation function: given (state, confidence) pairs from a
    recall's hits, the fraction of achievable confidence that is
    REINFORCED. Exact Fraction end to end.

        density = sum(confidence of REINFORCED hits) / len(hits)

    NEUTRAL and FORGOTTEN hits contribute zero but count in the
    denominator — resonance is what the evidence AFFIRMS, diluted by
    everything else the agent saw. No hits means no resonance: 0.
    """
    pairs = list(pairs)
    if not pairs:
        return Fraction(0)
    total = Fraction(0)
    for i, (state, confidence) in enumerate(pairs):
        if state not in ("REINFORCED", "NEUTRAL", "FORGOTTEN"):
            raise ValueError(f"pairs[{i}]: unknown state {state!r}.")
        _require_fraction(confidence, f"pairs[{i}].confidence")
        if not (0 <= confidence <= 1):
            raise ValueError(f"pairs[{i}]: confidence {confidence} outside "
                             "[0,1] — treat as corruption.")
        if state == "REINFORCED":
            total += confidence
    return total / Fraction(len(pairs))


def step(
    state: ControllerState,
    observation: Fraction,
    params: ControllerParams = DEFAULT_PARAMS,
) -> Decision:
    """
    THE decision function. One EMA update, one hysteresis evaluation,
    pure Fraction, no side effects — adversarially testable without a
    database, and observe() calls exactly this (no private copy of the
    math; the REC-010 lesson applied preemptively).

    Precedence when DWELLING and both exits fire in one step: the ema
    threshold wins over stagnation — the trigger should name the more
    informative cause (the field went cold, not merely flat).

    Stagnation is a DWELLING concept: while dwelling, an EMA that fails
    to improve by at least min_improvement increments the counter;
    improvement resets it; roaming never accumulates it; any transition
    zeroes it.
    """
    if state.mode not in MODES:
        raise ValueError(f"unknown mode {state.mode!r} — schema modes are {MODES}.")
    _require_fraction(state.ema, "state.ema")
    _require_fraction(state.ema_prev, "state.ema_prev")
    _require_fraction(observation, "observation")
    if not (0 <= observation <= 1):
        raise ValueError(f"observation {observation} outside [0,1].")
    if not (0 <= state.ema <= 1) or not (0 <= state.ema_prev <= 1):
        raise ValueError("ema outside [0,1] — the schema should have made "
                         "this impossible; treat as corruption.")
    if isinstance(state.stagnation_count, bool) or not isinstance(state.stagnation_count, int):
        raise TypeError("stagnation_count must be int.")
    if state.stagnation_count < 0:
        raise ValueError("stagnation_count must be >= 0.")
    if not isinstance(params, ControllerParams):
        raise TypeError(f"params must be ControllerParams, got "
            f"{type(params).__name__}.")

    new_ema = params.beta * observation + (1 - params.beta) * state.ema

    if state.mode == "DWELLING":
        improved = new_ema >= state.ema + params.min_improvement
        stagnation = 0 if improved else state.stagnation_count + 1
    else:
        stagnation = 0

    mode, trigger = state.mode, None
    if state.mode == "ROAMING" and new_ema >= params.enter:
        mode, trigger = "DWELLING", "ema_reached_enter_threshold"
    elif state.mode == "DWELLING" and new_ema <= params.exit:
        mode, trigger = "ROAMING", "ema_fell_to_exit_threshold"
    elif state.mode == "DWELLING" and stagnation >= params.stagnation_limit:
        mode, trigger = "ROAMING", "stagnation"

    transitioned = mode != state.mode
    if transitioned:
        stagnation = 0

    return Decision(
        state=ControllerState(
            mode=mode,
            ema=new_ema,
            ema_prev=state.ema,
            stagnation_count=stagnation,
        ),
        transitioned=transitioned,
        trigger=trigger,
    )


@dataclass(frozen=True)
class ObserveResult:
    agent_id: str
    decision: Decision
    current_region: str | None
    audited: bool


def observe(
    cur,
    *,
    node_id: str,
    agent_id: str,
    observation: Fraction,
    candidate_region: str | None = None,
    params: ControllerParams = DEFAULT_PARAMS,
) -> ObserveResult:
    """
    Apply one observation to agent_id's controller state, in the
    caller's transaction. Reads the row FOR UPDATE (or starts from
    INITIAL_STATE for a first-ever observation), decides via step(),
    writes back, and — ONLY if the mode transitioned — seals
    AGENT_MODE_CHANGED with the named trigger.

    Region semantics:
      - entering DWELLING requires candidate_region (the region whose
        density the observation measured — an agent cannot dwell
        nowhere); rejected with ValueError before any write if absent.
      - staying DWELLING keeps the current region (re-dwelling elsewhere
        is exit + enter, two audited transitions, not a silent hop).
      - ROAMING has no region: current_region goes NULL.

    updated_at is NEVER written here — the schema's ON UPDATE now()
    owns it, and writing it directly would suppress the expression
    (schema header, caveat 1).

    Raises:
      LookupError        — candidate_region does not exist (FK 23503
                           translated at the boundary, REC-013 pattern).
      StateWriteConflict — concurrent first-observations raced on this
                           agent's PK; retry the whole transaction.
    """
    if not agent_id or not agent_id.strip():
        raise ValueError("agent_id is required and cannot be empty.")
    _require_fraction(observation, "observation")
    if not isinstance(params, ControllerParams):
        raise TypeError(f"params must be ControllerParams, got "
                        f"{type(params).__name__}.")

    require_active_node(cur, node_id=node_id)

    cur.execute(
        """
        SELECT mode, current_region, confidence_ema, ema_prev, stagnation_count
          FROM agent_search_state
         WHERE agent_id = %s
           FOR UPDATE
        """,
        (agent_id,),
    )
    row = cur.fetchone()
    if row is None:
        state, region, is_new = INITIAL_STATE, None, True
    else:
        mode, region, ema_dec, ema_prev_dec, stagnation = row
        state = ControllerState(
            mode=mode,
            ema=Fraction(ema_dec),           # exact: Decimal -> Fraction
            ema_prev=Fraction(ema_prev_dec),
            stagnation_count=stagnation,
        )
        is_new = False

    decision = step(state, observation, params)
    new = decision.state

    if new.mode == "DWELLING":
        if decision.transitioned:
            if candidate_region is None:
                raise ValueError(
                    f"agent {agent_id!r} crossed the enter threshold but no "
                    "candidate_region was provided — an agent cannot dwell "
                    "nowhere. Nothing changed."
                )
            new_region = candidate_region
        else:
            new_region = region
    else:
        new_region = None

    q_ema = quantize(new.ema, "confidence_ema")
    q_prev = quantize(new.ema_prev, "ema_prev")

    try:
        if is_new:
            cur.execute(
                """
                INSERT INTO agent_search_state
                    (agent_id, mode, current_region,
                     confidence_ema, ema_prev, stagnation_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (agent_id, new.mode, new_region, q_ema, q_prev,
                 new.stagnation_count),
            )
        else:
            cur.execute(
                """
                UPDATE agent_search_state
                   SET mode = %s,
                       current_region = %s,
                       confidence_ema = %s,
                       ema_prev = %s,
                       stagnation_count = %s
                 WHERE agent_id = %s
                """,
                (new.mode, new_region, q_ema, q_prev,
                 new.stagnation_count, agent_id),
            )
    except Exception as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate is None:
            diag = getattr(exc, "diag", None)
            sqlstate = getattr(diag, "sqlstate", None)
        if sqlstate == "23503":  # foreign_key_violation
            raise LookupError(
                f"candidate_region {new_region!r} does not exist."
            ) from exc
        if sqlstate == "23505":  # unique_violation: racing first inserts
            raise StateWriteConflict(
                f"concurrent first-observation race on agent {agent_id!r}. "
                "Retry the entire transaction, not just this call."
            ) from exc
        raise

    audited = False
    if decision.transitioned:
        append_event(
            cur,
            node_id=node_id,
            event_type="AGENT_MODE_CHANGED",
            reason=decision.trigger,
            payload={
                "agent_id": agent_id,
                "old_mode": state.mode,
                "new_mode": new.mode,
                "region": new_region,
                "observation": quantize(observation, "observation"),
                "ema": q_ema,
                "ema_prev": q_prev,
                "stagnation_count": new.stagnation_count,
                "trigger": decision.trigger,
            },
        )
        audited = True

    return ObserveResult(
        agent_id=agent_id,
        decision=decision,
        current_region=new_region,
        audited=audited,
    )
