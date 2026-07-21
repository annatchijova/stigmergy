"""Pure boundary tests for the closed STIGMERGY authority vocabulary."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.authority import VALID_REGION_CAPABILITIES, validate_node_id, validate_region_capability

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  ok    {name}")
    except AssertionError as exc:
        failures.append(name)
        print(f"  FAIL  {name}: {exc}")
    except Exception as exc:
        failures.append(name)
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}")


def expect_raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:
        raise AssertionError(
            f"expected {exc_type.__name__}, got {type(exc).__name__}: {exc}"
        )
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


def t_node_ids_are_closed_and_bounded():
    assert validate_node_id("lambda-resolver_1") == "lambda-resolver_1"
    for bad in ("", " ", "bad id", "../node", "n" * 65, 7):
        expect_raises(ValueError, lambda bad=bad: validate_node_id(bad))


def t_capabilities_are_a_closed_vocabulary():
    assert validate_region_capability("SIGNAL") == "SIGNAL"
    assert "SIGNAL" in VALID_REGION_CAPABILITIES
    for bad in ("", "DISPATCH", "*", None, 7):
        expect_raises(ValueError, lambda bad=bad: validate_region_capability(bad))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
