"""
Pure-function tests for ops.memories — no database. The reinforcement
rule is the decision function of this module; its properties get pinned
here adversarially. Transactional paths are integration tests against
the cluster.
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal
from fractions import Fraction

from ops.memories import (
    DEFAULT_CONFIDENCE, REINFORCEMENT_ALPHA, reinforce_confidence, _content_sha256,
)
from audit.canonical import quantize, canonical_json

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

# --- reinforcement rule ------------------------------------------------------

def t_exactness_no_float_anywhere():
    c = reinforce_confidence(Fraction(1, 2))
    assert isinstance(c, Fraction) and c == Fraction(3, 5)  # 1/2 + 1/2 * 1/5

def t_monotone_strictly_increasing_below_one():
    for num, den in [(0,1),(1,7),(1,2),(9,10),(999,1000)]:
        c = Fraction(num, den)
        assert reinforce_confidence(c) > c, f"not increasing at {c}"

def t_fixed_point_exactly_one():
    assert reinforce_confidence(Fraction(1)) == Fraction(1)

def t_bounded_never_exceeds_one():
    c = Fraction(0)
    for _ in range(200):
        c = reinforce_confidence(c)
        assert 0 <= c <= 1
    assert c < 1  # approaches, never reaches, from below

def t_deterministic_across_paths():
    # applying n steps must equal the closed form 1 - (1-c0)*(1-a)^n
    c0, a, n = Fraction(1, 3), REINFORCEMENT_ALPHA, 17
    c = c0
    for _ in range(n):
        c = reinforce_confidence(c)
    assert c == 1 - (1 - c0) * (1 - a) ** n

def t_rejects_float():
    expect_raises(TypeError, lambda: reinforce_confidence(0.5))

def t_rejects_out_of_range():
    expect_raises(ValueError, lambda: reinforce_confidence(Fraction(3, 2)))
    expect_raises(ValueError, lambda: reinforce_confidence(Fraction(-1, 2)))

def t_default_confidence_matches_schema_default():
    assert format(quantize(DEFAULT_CONFIDENCE), "f") == "0.5000000000"

# --- payload pieces ----------------------------------------------------------

def t_content_sha256_known_vector():
    import hashlib
    assert _content_sha256("memoria") == hashlib.sha256("memoria".encode()).hexdigest()

def t_reinforce_payload_is_canonicalizable():
    # the exact payload shape reinforce() builds must pass canonical_json
    p = {
        "memory_id": "x", "old_state": "NEUTRAL", "new_state": "REINFORCED",
        "old_confidence": quantize(Fraction(1, 2)),
        "new_confidence": quantize(reinforce_confidence(Fraction(1, 2))),
        "alpha": "1/5",
    }
    s = canonical_json(p)
    assert '"new_confidence":"0.6000000000"' in s, s

def t_store_payload_confidence_is_decimal_at_scale():
    conf = quantize(DEFAULT_CONFIDENCE, "confidence")
    assert isinstance(conf, Decimal)
    s = canonical_json({"confidence": conf})
    assert s == '{"confidence":"0.5000000000"}'



# --- collective audit round 2 (OPS-012, OPS-013, OPS-011) --------------------

def t_alpha_float_rejected_no_contamination():
    # OPS-013 elevated: float alpha passed range check and contaminated
    # the exact arithmetic (Fraction * float -> float). Now rejected.
    expect_raises(TypeError, lambda: reinforce_confidence(Fraction(1, 2), alpha=0.3))

def t_alpha_out_of_range_rejected():
    expect_raises(ValueError, lambda: reinforce_confidence(Fraction(1, 2), alpha=Fraction(2)))
    expect_raises(ValueError, lambda: reinforce_confidence(Fraction(1, 2), alpha=Fraction(0)))
    expect_raises(ValueError, lambda: reinforce_confidence(Fraction(1, 2), alpha=Fraction(1)))

def t_result_is_always_fraction():
    r = reinforce_confidence(Fraction(1, 2))
    assert isinstance(r, Fraction), type(r).__name__

def t_stored_confidence_saturates_at_step_104():
    # From c0 = 1/2 with alpha = 1/5, quantization at scale 10 reaches
    # exactly 1 at step 104 (closed form n >= log(1e-10)/log(0.8) ~ 103.2).
    # The Fraction itself never reaches 1 — only its stored projection.
    c = Fraction(1, 2)
    for n in range(1, 105):
        c = reinforce_confidence(c)
        if n == 103:
            assert quantize(c) < 1, f"saturated early at {n}"
    assert quantize(c) == 1
    assert c < 1  # the exact value still hasn't — and never will


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
