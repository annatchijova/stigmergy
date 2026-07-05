"""
Pure-function tests for ops.controller — the hysteresis decision function,
the density observation, and the parameter boundaries. Adversarial on the
property that matters: the open band between exit and enter is where
NOTHING transitions, and the arithmetic is exact enough to test AT the
boundaries, not near them.
"""

from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fractions import Fraction

from ops.controller import (
    DEFAULT_PARAMS, INITIAL_STATE, ControllerParams, ControllerState,
    observe, resonance_density, step,
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

F = Fraction

def st(mode="ROAMING", ema=F(0), prev=F(0), stag=0):
    return ControllerState(mode=mode, ema=ema, ema_prev=prev, stagnation_count=stag)

# The reference params used throughout: beta 1/4, enter 3/5, exit 2/5,
# stagnation 3, min improvement 1/100 — small stagnation limit so tests
# trip it quickly.
P = ControllerParams(beta=F(1, 4), enter=F(3, 5), exit=F(2, 5),
                     stagnation_limit=3, min_improvement=F(1, 100))

def obs_for_ema(target, current, beta=F(1, 4)):
    """Solve beta*obs + (1-beta)*ema = target for obs — lets tests land
    the new EMA EXACTLY on a threshold, which only exact arithmetic
    permits."""
    return (target - (1 - beta) * current) / beta

# --- hysteresis: the property itself ------------------------------------------

def t_equal_thresholds_rejected_the_hysteresis_pin():
    # exit == enter is zero hysteresis: oscillation at a single boundary.
    expect_raises(ValueError, lambda: ControllerParams(enter=F(1, 2), exit=F(1, 2)))
    expect_raises(ValueError, lambda: ControllerParams(enter=F(2, 5), exit=F(3, 5)))

def t_open_band_transitions_neither_mode():
    # The same EMA value strictly inside (exit, enter) keeps BOTH modes
    # where they are — that band IS the hysteresis.
    target = F(1, 2)  # between 2/5 and 3/5
    for mode in ("ROAMING", "DWELLING"):
        s = st(mode=mode, ema=F(1, 2))
        d = step(s, obs_for_ema(target, s.ema), P)
        assert d.state.ema == target
        assert d.transitioned is False, mode
        assert d.state.mode == mode

def t_roaming_enters_dwelling_exactly_at_enter_inclusive():
    s = st("ROAMING", ema=F(1, 2))
    d = step(s, obs_for_ema(F(3, 5), s.ema), P)  # new ema == enter exactly
    assert d.state.ema == F(3, 5)
    assert d.transitioned and d.state.mode == "DWELLING"
    assert d.trigger == "ema_reached_enter_threshold"

def t_dwelling_exits_exactly_at_exit_inclusive():
    s = st("DWELLING", ema=F(1, 2))
    d = step(s, obs_for_ema(F(2, 5), s.ema), P)  # new ema == exit exactly
    assert d.state.ema == F(2, 5)
    assert d.transitioned and d.state.mode == "ROAMING"
    assert d.trigger == "ema_fell_to_exit_threshold"

def t_one_billionth_inside_the_band_does_not_transition():
    eps = F(1, 10**9)
    s = st("ROAMING", ema=F(1, 2))
    d = step(s, obs_for_ema(F(3, 5) - eps, s.ema), P)
    assert d.transitioned is False
    s = st("DWELLING", ema=F(1, 2))
    d = step(s, obs_for_ema(F(2, 5) + eps, s.ema), P)
    assert d.transitioned is False

def t_no_oscillation_across_the_band():
    # Enter dwelling, then feed the exact observation that would sit a
    # hair BELOW enter: a zero-hysteresis controller would flip back;
    # this one stays dwelling until EXIT is reached.
    s = st("ROAMING", ema=F(1, 2))
    d = step(s, obs_for_ema(F(3, 5), s.ema), P)
    assert d.state.mode == "DWELLING"
    d2 = step(d.state, obs_for_ema(F(3, 5) - F(1, 100), d.state.ema), P)
    assert d2.state.mode == "DWELLING" and d2.transitioned is False

# --- ema arithmetic ------------------------------------------------------------

def t_ema_update_exact():
    d = step(st("ROAMING", ema=F(1, 2)), F(1), P)
    assert d.state.ema == F(1, 4) * 1 + F(3, 4) * F(1, 2) == F(5, 8)

def t_ema_fixed_point_when_observation_equals_ema():
    d = step(st("ROAMING", ema=F(1, 3)), F(1, 3), P)
    assert d.state.ema == F(1, 3)  # beta*e + (1-beta)*e = e, exactly

def t_ema_prev_is_the_pre_update_value():
    d = step(st("ROAMING", ema=F(1, 2)), F(1), P)
    assert d.state.ema_prev == F(1, 2)

def t_ema_closed_form_convergence():
    # n observations of 1 from ema 0: ema_n = 1 - (1-beta)^n, exactly.
    s, n = st("ROAMING"), 17
    for _ in range(n):
        s = step(s, F(1), P).state
    assert s.ema == 1 - (F(3, 4)) ** n

# --- stagnation -----------------------------------------------------------------

def t_flat_dwelling_stagnates_and_exits_at_limit():
    # obs == ema keeps the EMA exactly flat: improvement 0 < 1/100.
    s = st("DWELLING", ema=F(1, 2))
    for expected in (1, 2):
        d = step(s, s.ema, P)
        assert d.state.stagnation_count == expected and not d.transitioned
        s = d.state
    d = step(s, s.ema, P)  # third flat step reaches limit 3
    assert d.transitioned and d.state.mode == "ROAMING"
    assert d.trigger == "stagnation"
    assert d.state.stagnation_count == 0  # transitions zero the counter

def t_improvement_below_min_still_counts_as_stagnation():
    s = st("DWELLING", ema=F(1, 2))
    tiny = obs_for_ema(s.ema + F(1, 200), s.ema)  # +1/200 < 1/100
    d = step(s, tiny, P)
    assert d.state.stagnation_count == 1

def t_sufficient_improvement_resets_stagnation():
    s = st("DWELLING", ema=F(1, 2), stag=2)
    d = step(s, obs_for_ema(s.ema + F(1, 100), s.ema), P)  # exactly min
    assert d.state.stagnation_count == 0

def t_roaming_never_accumulates_stagnation():
    d = step(st("ROAMING", ema=F(1, 2), stag=0), F(1, 2), P)
    assert d.state.stagnation_count == 0

def t_exit_threshold_wins_over_stagnation_precedence_pin():
    # One step where the EMA falls to exit AND stagnation hits its
    # limit: the trigger names the ema threshold — the field went cold,
    # not merely flat.
    s = st("DWELLING", ema=F(1, 2), stag=2)
    d = step(s, obs_for_ema(F(2, 5), s.ema), P)
    assert d.transitioned and d.trigger == "ema_fell_to_exit_threshold"

# --- resonance_density -----------------------------------------------------------

def t_density_empty_is_zero():
    assert resonance_density([]) == F(0)

def t_density_all_reinforced_full_confidence_is_one():
    pairs = [("REINFORCED", F(1))] * 4
    assert resonance_density(pairs) == F(1)

def t_density_dilution_by_non_reinforced():
    pairs = [("REINFORCED", F(1)), ("NEUTRAL", F(1)), ("FORGOTTEN", F(1))]
    assert resonance_density(pairs) == F(1, 3)  # exact thirds, no drift

def t_density_neutral_confidence_contributes_nothing():
    pairs = [("NEUTRAL", F(1)), ("NEUTRAL", F(1))]
    assert resonance_density(pairs) == F(0)

def t_density_rejects_float_confidence():
    expect_raises(TypeError, lambda: resonance_density([("REINFORCED", 0.5)]))

def t_density_rejects_unknown_state():
    expect_raises(ValueError, lambda: resonance_density([("ORPHANED", F(1))]))

def t_density_rejects_out_of_range_confidence():
    expect_raises(ValueError, lambda: resonance_density([("REINFORCED", F(3, 2))]))

# --- contamination and boundary guards --------------------------------------------

def t_step_rejects_float_observation():
    expect_raises(TypeError, lambda: step(st(), 0.5, P))

def t_step_rejects_out_of_range_observation():
    expect_raises(ValueError, lambda: step(st(), F(3, 2), P))

def t_step_rejects_float_ema_in_state():
    bad = ControllerState(mode="ROAMING", ema=0.5, ema_prev=F(0), stagnation_count=0)
    expect_raises(TypeError, lambda: step(bad, F(1, 2), P))

def t_step_rejects_unknown_mode():
    bad = ControllerState(mode="WANDERING", ema=F(0), ema_prev=F(0), stagnation_count=0)
    expect_raises(ValueError, lambda: step(bad, F(1, 2), P))

def t_params_reject_float_everything():
    expect_raises(TypeError, lambda: ControllerParams(beta=0.25))
    expect_raises(TypeError, lambda: ControllerParams(enter=0.6))
    expect_raises(TypeError, lambda: ControllerParams(exit=0.4))
    expect_raises(TypeError, lambda: ControllerParams(min_improvement=0.01))

def t_params_reject_bad_ranges():
    expect_raises(ValueError, lambda: ControllerParams(beta=F(0)))
    expect_raises(ValueError, lambda: ControllerParams(beta=F(1)))
    expect_raises(ValueError, lambda: ControllerParams(stagnation_limit=0))
    expect_raises(TypeError, lambda: ControllerParams(stagnation_limit=True))
    expect_raises(ValueError, lambda: ControllerParams(min_improvement=F(-1, 100)))

def t_initial_state_matches_schema_defaults():
    assert INITIAL_STATE.mode == "ROAMING"
    assert INITIAL_STATE.ema == F(0) and INITIAL_STATE.ema_prev == F(0)
    assert INITIAL_STATE.stagnation_count == 0

def t_default_params_are_valid_and_hysteretic():
    assert DEFAULT_PARAMS.exit < DEFAULT_PARAMS.enter

# --- observe validators fire before any cursor use ---------------------------------

def t_observe_rejects_empty_agent_before_touching_db():
    expect_raises(ValueError, lambda: observe(
        None, node_id="n", agent_id="  ", observation=F(1, 2)))

def t_observe_rejects_float_observation_before_touching_db():
    expect_raises(TypeError, lambda: observe(
        None, node_id="n", agent_id="a", observation=0.5))

def t_observe_rejects_non_params_before_touching_db():
    expect_raises(TypeError, lambda: observe(
        None, node_id="n", agent_id="a", observation=F(1, 2), params={"beta": F(1, 4)}))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
