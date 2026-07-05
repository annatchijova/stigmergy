"""
Pure-function tests for ops.orphans — boundary validators and the exact
window decomposition. The sweep itself (staleness SQL, locked read,
tier transition, event payload) is transactional and goes to the
integration checklist, per the project's test split.
"""

from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from fractions import Fraction

from ops.orphans import (
    DEFAULT_ELIGIBLE_STATES, DEFAULT_STALENESS_WINDOW, SWEEP_TIERS,
    VALID_STATES, _validate_states, _window_microseconds, sweep_orphans,
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

KW = dict(node_id="n")

# --- design decisions pinned as constants -------------------------------------

def t_global_never_sweeps():
    assert "GLOBAL" not in SWEEP_TIERS
    assert "ORPHANED" not in SWEEP_TIERS
    assert SWEEP_TIERS == ("SHORT_TERM", "REGIONAL")

def t_reinforced_protected_by_default():
    assert "REINFORCED" not in DEFAULT_ELIGIBLE_STATES
    assert DEFAULT_ELIGIBLE_STATES == ("NEUTRAL", "FORGOTTEN")

def t_default_window_is_a_week():
    assert DEFAULT_STALENESS_WINDOW == timedelta(days=7)

def t_valid_states_mirror_schema():
    assert VALID_STATES == ("REINFORCED", "NEUTRAL", "FORGOTTEN")

# --- window boundary -----------------------------------------------------------

def t_window_must_be_timedelta():
    expect_raises(TypeError, lambda: sweep_orphans(None, window=7, **KW))
    expect_raises(TypeError, lambda: sweep_orphans(None, window=604800.0, **KW))

def t_window_must_be_positive():
    expect_raises(ValueError, lambda: sweep_orphans(None, window=timedelta(0), **KW))
    expect_raises(ValueError, lambda: sweep_orphans(None, window=timedelta(days=-1), **KW))

def t_window_microseconds_exact():
    td = timedelta(days=2, seconds=3, microseconds=7)
    assert _window_microseconds(td) == 2 * 86_400_000_000 + 3_000_000 + 7
    assert isinstance(_window_microseconds(td), int)

def t_window_microseconds_no_float_anywhere():
    # total_seconds() would have been a float; the decomposition is exact
    # for values where float would already have lost the microsecond.
    td = timedelta(days=106_751, microseconds=1)  # large + tiny
    us = _window_microseconds(td)
    assert us == 106_751 * 86_400_000_000 + 1
    assert us != int(td.total_seconds()) * 1_000_000  # float dropped the 1us

# --- limit boundary (REC-012 discipline) ----------------------------------------

def t_limit_rejects_bool_and_non_int():
    expect_raises(TypeError, lambda: sweep_orphans(None, limit=True, **KW))
    expect_raises(TypeError, lambda: sweep_orphans(None, limit=10.0, **KW))

def t_limit_rejects_nonpositive():
    expect_raises(ValueError, lambda: sweep_orphans(None, limit=0, **KW))
    expect_raises(ValueError, lambda: sweep_orphans(None, limit=-1, **KW))

# --- eligible_states boundary ----------------------------------------------------

def t_states_bare_string_rejected():
    # 'NEUTRAL' iterates as letters — matching nothing, silently.
    expect_raises(TypeError, lambda: sweep_orphans(None, eligible_states="NEUTRAL", **KW))

def t_states_empty_rejected():
    expect_raises(ValueError, lambda: sweep_orphans(None, eligible_states=(), **KW))

def t_states_unknown_rejected():
    expect_raises(ValueError, lambda: sweep_orphans(None, eligible_states=("NEUTRAL", "FORGOTEN"), **KW))

def t_states_duplicate_rejected():
    expect_raises(ValueError, lambda: sweep_orphans(None, eligible_states=("NEUTRAL", "NEUTRAL"), **KW))

def t_states_non_str_rejected():
    expect_raises(TypeError, lambda: sweep_orphans(None, eligible_states=("NEUTRAL", 3), **KW))

def t_reinforced_sweepable_only_when_explicit():
    # The validator ACCEPTS an explicit policy including REINFORCED —
    # the protection is a default, not a prohibition (module docstring).
    assert _validate_states(("NEUTRAL", "REINFORCED")) == ("NEUTRAL", "REINFORCED")

def t_validate_states_preserves_caller_order():
    assert _validate_states(["FORGOTTEN", "NEUTRAL"]) == ("FORGOTTEN", "NEUTRAL")

# --- now boundary -----------------------------------------------------------------

def t_naive_now_rejected():
    expect_raises(ValueError, lambda: sweep_orphans(None, now=datetime(2026, 7, 4), **KW))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
