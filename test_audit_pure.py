"""
Pure-function tests for the audit package — no database required.
The transactional paths (append_event, verify_chain, create_snapshot)
get their integration tests against the real cluster; everything a hash
depends on is testable right here, and adversarially.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit import (
    GENESIS_PREV_HASH,
    LEDGER_GENESIS_HASH,
    canonical_json,
    compute_entry_hash,
    merkle_root_over_heads,
    quantize,
)

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  ok    {name}")
    except AssertionError as e:
        failures.append(name)
        print(f"  FAIL  {name}: {e}")
    except Exception as e:
        failures.append(name)
        print(f"  ERROR {name}: {type(e).__name__}: {e}")


def expect_raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return
    except Exception as e:
        raise AssertionError(f"expected {exc_type.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


# --- canonical_json ---------------------------------------------------------

def t_sorted_keys():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

def t_no_whitespace():
    s = canonical_json({"a": [1, 2], "b": {"c": "x"}})
    assert " " not in s and "\n" not in s

def t_float_rejected():
    expect_raises(TypeError, lambda: canonical_json({"x": 0.5}))

def t_nested_float_rejected():
    expect_raises(TypeError, lambda: canonical_json({"a": {"b": [1, 2.0]}}))

def t_decimal_at_scale_serializes_as_string():
    d = quantize(Fraction(1, 3))
    s = canonical_json({"v": d})
    assert s == '{"v":"0.3333333333"}', s

def t_decimal_off_scale_rejected():
    expect_raises(ValueError, lambda: canonical_json({"v": Decimal("0.5")}))

def t_unicode_not_escaped():
    assert canonical_json({"t": "vigía"}) == '{"t":"vigía"}'

def t_same_content_same_bytes():
    a = canonical_json({"x": 1, "y": {"b": 2, "a": 3}})
    b = canonical_json({"y": {"a": 3, "b": 2}, "x": 1})
    assert a == b

def t_non_str_key_rejected():
    expect_raises(TypeError, lambda: canonical_json({1: "x"}))

# --- quantize ---------------------------------------------------------------

def t_quantize_fraction_exact():
    assert format(quantize(Fraction(1, 4)), "f") == "0.2500000000"

def t_quantize_half_even():
    # 0.00000000005 -> banker's rounding to scale 10: rounds to even digit
    assert format(quantize(Fraction(5, 10**11)), "f") == "0.0000000000"
    assert format(quantize(Fraction(15, 10**11)), "f") == "0.0000000002"

def t_quantize_rejects_float():
    expect_raises(TypeError, lambda: quantize(0.5))

def t_quantize_rejects_bool():
    expect_raises(TypeError, lambda: quantize(True))

# --- compute_entry_hash -----------------------------------------------------

TS = datetime(2026, 7, 3, 12, 0, 0, 123456, tzinfo=timezone.utc)

def _hash(**over):
    kw = dict(node_id="n1", seq=0, prev_hash=GENESIS_PREV_HASH,
              event_type="MEMORY_STORED", reason="test",
              created_at=TS, payload={"k": "v"})
    kw.update(over)
    return compute_entry_hash(**kw)[0]

def t_hash_deterministic():
    assert _hash() == _hash()

def t_hash_sensitive_to_every_field():
    base = _hash()
    assert _hash(seq=1) != base
    assert _hash(node_id="n2") != base
    assert _hash(event_type="X") != base
    assert _hash(reason="other") != base
    assert _hash(payload={"k": "w"}) != base
    assert _hash(prev_hash="0" * 64) != base
    assert _hash(created_at=TS.replace(microsecond=123457)) != base

def t_hash_naive_datetime_rejected():
    expect_raises(ValueError, lambda: _hash(created_at=datetime(2026, 1, 1)))

def t_hash_payload_key_order_irrelevant():
    assert _hash(payload={"a": 1, "b": 2}) == _hash(payload={"b": 2, "a": 1})

def t_genesis_constant_documented_value():
    import hashlib
    assert GENESIS_PREV_HASH == hashlib.sha256(b"STIGMERGY_GENESIS").hexdigest()
    assert LEDGER_GENESIS_HASH == hashlib.sha256(b"STIGMERGY_LEDGER_GENESIS").hexdigest()
    assert GENESIS_PREV_HASH != LEDGER_GENESIS_HASH  # distinct domains, distinct constants

# --- merkle -----------------------------------------------------------------

def t_merkle_deterministic_and_order_free():
    h1 = merkle_root_over_heads({"a": "h1", "b": "h2", "c": "h3"})
    h2 = merkle_root_over_heads({"c": "h3", "a": "h1", "b": "h2"})
    assert h1 == h2

def t_merkle_sensitive_to_heads():
    base = merkle_root_over_heads({"a": "h1", "b": "h2"})
    assert merkle_root_over_heads({"a": "h1", "b": "hX"}) != base
    assert merkle_root_over_heads({"a": "h1"}) != base

def t_merkle_single_node():
    # One node is legal (a one-leaf tree is its own root at leaf level).
    r = merkle_root_over_heads({"solo": "h"})
    assert isinstance(r, str) and len(r) == 64

def t_merkle_no_duplication_ambiguity():
    # With last-leaf duplication (Bitcoin-style), {a,b,b} and {a,b} can
    # collide. Promotion must keep them distinct.
    two = merkle_root_over_heads({"a": "h1", "b": "h2"})
    three = merkle_root_over_heads({"a": "h1", "b": "h2", "c": "h2"})
    assert two != three

def t_merkle_empty_rejected():
    expect_raises(ValueError, lambda: merkle_root_over_heads({}))




# --- fixes from collective audit (AC-101, AC-304, AC-305, AC-203) -----------

from audit.chain import _format_ts as _fmt
from datetime import datetime as _dt, timezone as _tz

def t_bool_allowed_in_payload_documented_policy():
    # AC-101: bool is JSON-native with one canonical form; allowed here,
    # rejected in quantize. Both by design, both pinned by test.
    assert canonical_json({"migrated": True, "active": False}) == '{"active":false,"migrated":true}'

def t_format_ts_preserves_microseconds():
    ts = _dt(2026, 7, 3, 12, 0, 0, 123456, tzinfo=_tz.utc)
    assert _fmt(ts) == "2026-07-03T12:00:00.123456+00:00", _fmt(ts)

def t_format_ts_zero_microseconds_still_six_digits():
    # timespec='microseconds' must emit .000000, never omit it — two
    # formats for one instant would be two representations.
    ts = _dt(2026, 7, 3, 12, 0, 0, 0, tzinfo=_tz.utc)
    assert _fmt(ts) == "2026-07-03T12:00:00.000000+00:00", _fmt(ts)

def t_format_ts_converts_to_utc():
    from datetime import timedelta
    art = _tz(timedelta(hours=-3))  # Buenos Aires, sin DST
    ts = _dt(2026, 7, 3, 9, 0, 0, 1, tzinfo=art)
    assert _fmt(ts) == "2026-07-03T12:00:00.000001+00:00", _fmt(ts)

def t_merkle_no_duplication_ambiguity_exhaustive():
    base = merkle_root_over_heads({"a": "h1", "b": "h2"})
    assert merkle_root_over_heads({"a": "h1", "b": "h2", "c": "h2"}) != base  # dup last
    assert merkle_root_over_heads({"a": "h1", "b": "h1", "c": "h2"}) != base  # dup first
    assert merkle_root_over_heads({"a": "h1", "b": "h2", "c": "h3"}) != base  # extra distinct

def t_merkle_rejects_non_string_heads():
    expect_raises(TypeError, lambda: merkle_root_over_heads({"a": 123}))
    expect_raises(TypeError, lambda: merkle_root_over_heads({1: "h"}))

def t_datetime_in_payload_gets_actionable_error():
    try:
        canonical_json({"ts": _dt.now(_tz.utc)})
    except TypeError as e:
        assert "_format_ts" in str(e), str(e)
    else:
        raise AssertionError("expected TypeError")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
