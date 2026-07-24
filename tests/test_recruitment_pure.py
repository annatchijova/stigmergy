"""
Pure-function tests for ops.recruitment — the consensus decision function
and the boundary validators. Adversarial on the math: the whole point of
this module is that the verdict is exact and float-free, so the tests try
to break exactly that. Extended across two collective audit rounds:
REC-002/005/006/007 (round 1) and REC-010/012/013/014 (round 2).
"""

from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import timedelta
from fractions import Fraction

from ops.recruitment import (
    CONSENSUS_QUORUM, LiveSignal, MemoryOrphaned, compute_consensus,
    emit_signal, expire_signals, resolve_recruitment,
)

failures = []

def check(name, fn):
    try:
        fn(); print(f"  ok    {name}")
    except AssertionError as e:
        failures.append(name); print(f"  FAIL  {name}: {e}")
    except Exception as e:
        failures.append(name); print(f"  ERROR {name}: {type(e).__name__}: {e}")

def expect_raises(exc_type, fn):
    try: fn()
    except exc_type: return
    except Exception as e:
        raise AssertionError(f"expected {exc_type.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")

def sig(v, sid="s", region="target"):
    return LiveSignal(signal_id=sid, origin_region=region,
                      vigor=v if isinstance(v, Fraction) else Fraction(v),
                      decayed_strength=1.0)

T = dict(target_region="target")

# --- consensus decision ------------------------------------------------------

def t_empty_never_reaches():
    assert compute_consensus([], **T) is False

def t_single_full_vigor_reaches_half_quorum():
    assert compute_consensus([sig(1)], **T) is True

def t_single_weak_vigor_below_quorum():
    assert compute_consensus([sig(Fraction(2, 5))], **T) is False

def t_two_half_vigor_exactly_meets():
    live = [sig(Fraction(1, 2), "a"), sig(Fraction(1, 2), "b")]
    assert compute_consensus(live, **T) is True

def t_boundary_is_inclusive():
    # exactly at quorum must count as reached (>=, not >)
    live = [sig(Fraction(1, 2), "a"), sig(Fraction(1, 2), "b")]
    assert compute_consensus(live, Fraction(1, 2), **T) is True

def t_exactness_no_float_drift():
    # three signals of 1/3 each: for = 1, max = 3, ratio = 1/3. With
    # quorum 1/3 this is EXACTLY at boundary — floats would make
    # 1/3+1/3+1/3 either 0.9999... or 1.0000...2 and the boundary would
    # flip. Fraction makes it exact.
    live = [sig(Fraction(1, 3), f"s{i}") for i in range(3)]
    assert compute_consensus(live, Fraction(1, 3), **T) is True
    assert compute_consensus(live, Fraction(1, 3) + Fraction(1, 10**9), **T) is False

def t_many_weak_signals_can_still_reach():
    live = [sig(Fraction(1, 2), f"s{i}") for i in range(10)]
    assert compute_consensus(live, **T) is True

def t_default_quorum_is_half():
    assert CONSENSUS_QUORUM == Fraction(1, 2)

# --- REC-002: target-filtered voting -----------------------------------------

def t_foreign_vigor_does_not_support_target():
    # One full-vigor signal FOR the target among three live: 1 >= 1/2 * 3?
    # No. Before the fix, all three would have voted for whatever target
    # the caller picked — cherry-picking a target claimed foreign vigor.
    live = [sig(1, "a", region="target"),
            sig(1, "b", region="elsewhere"),
            sig(1, "c", region="elsewhere")]
    assert compute_consensus(live, **T) is False

def t_majority_of_field_pointing_here_reaches():
    live = [sig(1, "a", region="target"),
            sig(1, "b", region="target"),
            sig(1, "c", region="elsewhere")]
    # 2 >= 1/2 * 3 -> reached
    assert compute_consensus(live, **T) is True

def t_competing_signals_dilute_exactly_at_boundary():
    # 1 full vote for target, 1 full vote elsewhere: 1 >= 1/2 * 2 exactly.
    live = [sig(1, "a", region="target"), sig(1, "b", region="elsewhere")]
    assert compute_consensus(live, **T) is True
    # ...and the tiniest extra competition flips it: 1 >= 1/2 * 3 fails.
    live = live + [sig(Fraction(1, 10**9), "c", region="elsewhere")]
    assert compute_consensus(live, **T) is False

def t_zero_votes_for_target_never_reaches():
    live = [sig(1, "a", region="elsewhere")]
    assert compute_consensus(live, **T) is False

# --- REC-010: target_region is required, one semantics only ------------------

def t_target_region_is_required():
    # The former target_region=None "everyone votes" mode is gone — a
    # decision function does not get to mean two things.
    expect_raises(TypeError, lambda: compute_consensus([sig(1)]))

def t_unanimous_field_for_target_is_the_only_all_vote_case():
    # What None used to fake is expressible honestly: every live signal
    # actually originates at the target.
    live = [sig(Fraction(1, 2), "a"), sig(Fraction(1, 2), "b")]
    assert compute_consensus(live, **T) is True

# --- REC-006 / REC-014: contamination, range, duplicate guards ---------------

def t_quorum_must_be_fraction():
    expect_raises(TypeError, lambda: compute_consensus([sig(1)], 0.5, **T))

def t_quorum_out_of_range_rejected():
    expect_raises(ValueError, lambda: compute_consensus([sig(1)], Fraction(0), **T))
    expect_raises(ValueError, lambda: compute_consensus([sig(1)], Fraction(3, 2), **T))
    expect_raises(ValueError, lambda: compute_consensus([sig(1)], Fraction(-1, 2), **T))

def t_quorum_of_exactly_one_is_legal_unanimity():
    assert compute_consensus([sig(1, "a"), sig(1, "b")], Fraction(1), **T) is True
    assert compute_consensus([sig(1, "a"), sig(Fraction(999, 1000), "b")],
                             Fraction(1), **T) is False

def t_float_vigor_rejected_no_contamination():
    # The OPS-013 class, one dataclass field away: a LiveSignal built with
    # a float vigor would turn sum() and the comparison into float math.
    bad = LiveSignal(signal_id="s", origin_region="r", vigor=0.5, decayed_strength=1.0)
    expect_raises(TypeError, lambda: compute_consensus([bad], **T))

def t_out_of_range_vigor_rejected_as_corruption():
    bad = LiveSignal(signal_id="s", origin_region="r",
                     vigor=Fraction(3, 2), decayed_strength=1.0)
    expect_raises(ValueError, lambda: compute_consensus([bad], **T))

def t_duplicate_signal_id_rejected():
    # REC-014: one signal, one vote. Two entries sharing an id would let
    # a signal vote twice — corruption inflating consensus.
    live = [sig(Fraction(1, 2), "dup"), sig(Fraction(1, 2), "dup", region="elsewhere")]
    expect_raises(ValueError, lambda: compute_consensus(live, **T))

def t_duplicate_rejected_even_when_it_would_not_change_verdict():
    # The guard is about integrity, not outcome: reject the duplicate
    # even if the verdict would be identical without it.
    live = [sig(1, "dup"), sig(1, "dup")]
    expect_raises(ValueError, lambda: compute_consensus(live, **T))

# --- REC-005 / REC-006: resolve validators fire before any cursor use --------

def t_resolve_rejects_float_quorum_before_touching_db():
    expect_raises(TypeError, lambda: resolve_recruitment(
        None, node_id="n", memory_id="m", target_region="t", quorum=0.5))

def t_resolve_rejects_bad_liveness_floor_before_touching_db():
    expect_raises(ValueError, lambda: resolve_recruitment(
        None, node_id="n", memory_id="m", target_region="t",
        liveness_floor=0.0))
    expect_raises(ValueError, lambda: resolve_recruitment(
        None, node_id="n", memory_id="m", target_region="t",
        liveness_floor=1.0))

# --- REC-007: emission boundary ----------------------------------------------

def t_emit_rejects_nonpositive_ttl_before_touching_db():
    kw = dict(node_id="n", origin_region="r", memory_id="m",
              reason="HIGH_RESONANCE", vigor=Fraction(1, 2))
    expect_raises(ValueError, lambda: emit_signal(None, ttl=timedelta(0), **kw))
    expect_raises(ValueError, lambda: emit_signal(None, ttl=timedelta(seconds=-1), **kw))

def t_emit_rejects_float_ttl_type():
    kw = dict(node_id="n", origin_region="r", memory_id="m",
              reason="HIGH_RESONANCE", vigor=Fraction(1, 2))
    expect_raises(TypeError, lambda: emit_signal(None, ttl=1800.0, **kw))

def t_emit_rejects_float_vigor():
    expect_raises(TypeError, lambda: emit_signal(
        None, node_id="n", origin_region="r", memory_id="m",
        reason="HIGH_RESONANCE", vigor=0.5))

def t_emit_rejects_unknown_reason():
    expect_raises(ValueError, lambda: emit_signal(
        None, node_id="n", origin_region="r", memory_id="m",
        reason="BECAUSE", vigor=Fraction(1, 2)))

# --- REC-018: an ORPHANED memory is not consensus's to resurrect -------------

class _FakeCursor:
    """
    Scripted (SELECT-result, ) queue standing in for a real cursor, just
    far enough to reach the ORPHANED guard: require_region_capability's
    three reads (agent_nodes, current_user, node_region_capabilities),
    then memory_regions.status, then the memories row itself. No fifth
    query (the live-signals SELECT) should ever be issued once the
    memory's tier comes back ORPHANED — that is the bug this guards.
    """
    def __init__(self, responses):
        self._responses = list(responses)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return self._responses.pop(0)

def t_resolve_rejects_orphaned_memory_before_touching_signals():
    cur = _FakeCursor([
        ("principal", "ACTIVE"),      # agent_nodes: db_principal, status
        ("principal",),                # current_user
        ("ACTIVE",),                   # node_region_capabilities.status
        ("ACTIVE",),                   # memory_regions.status (target)
        ("some-region", "ORPHANED"),   # memories: region_id, tier
    ])
    expect_raises(MemoryOrphaned, lambda: resolve_recruitment(
        cur, node_id="n", memory_id="m", target_region="other-region"))
    assert len(cur.executed) == 5, (
        f"expected exactly 5 queries (stopping at the memories row), "
        f"got {len(cur.executed)}: {cur.executed}"
    )

# --- REC-012: sweep limit boundary --------------------------------------------

def t_expire_rejects_nonpositive_limit_before_touching_db():
    expect_raises(ValueError, lambda: expire_signals(None, node_id="n", limit=0))
    expect_raises(ValueError, lambda: expire_signals(None, node_id="n", limit=-5))

def t_expire_rejects_bool_and_non_int_limit():
    # bool is an int subclass by historical accident — same rejection
    # discipline as quantize().
    expect_raises(TypeError, lambda: expire_signals(None, node_id="n", limit=True))
    expect_raises(TypeError, lambda: expire_signals(None, node_id="n", limit=10.0))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
