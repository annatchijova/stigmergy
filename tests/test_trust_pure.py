"""
Pure-function tests for ops.trust's sealing math — no database required.
quarantine_actor/quarantine_memory/rehabilitate_memory are fully
transactional (SELECT/UPDATE/INSERT against agent_nodes, custody_chain,
memories, taint_sweeps) and belong in the integration checklist
(tests/INTEGRATION_CHECKLIST.md), not here. What IS pure and adversarial
is the sealing scheme every sweep's evidence depends on: the claim "we
flagged exactly these memory_ids" must hash to one value regardless of
row-fetch order, and must change if a single id is added, removed, or
altered.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.trust import _seal_ids

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


A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"


def t_seal_deterministic():
    assert _seal_ids([A, B, C]) == _seal_ids([A, B, C])

def t_seal_order_dependent_input_must_be_presorted():
    # _seal_ids itself does not sort — callers pass a pre-sorted list
    # (quarantine_actor's ORDER BY memory_id ASC). Two DIFFERENT orders
    # of the same set therefore produce DIFFERENT seals unless sorted
    # first — this pins that quarantine_actor's sort is load-bearing,
    # not decorative.
    assert _seal_ids([A, B, C]) != _seal_ids([C, B, A])
    assert _seal_ids(sorted([C, B, A])) == _seal_ids(sorted([A, B, C]))

def t_seal_sensitive_to_membership():
    base = _seal_ids([A, B])
    assert _seal_ids([A, B, C]) != base
    assert _seal_ids([A]) != base

def t_seal_sensitive_to_single_char():
    base = _seal_ids([A, B, C])
    tampered = A[:-1] + ("0" if A[-1] != "0" else "1")
    assert _seal_ids([tampered, B, C]) != base

def t_seal_empty_set_is_stable():
    assert _seal_ids([]) == _seal_ids([])

def t_seal_is_hex_sha256():
    s = _seal_ids([A, B, C])
    assert len(s) == 64
    int(s, 16)  # raises ValueError if not hex


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
