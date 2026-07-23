"""
Pure-function tests for audit.custody — no database required. Mirrors
tests/test_audit_pure.py's harness: everything a hash depends on is
testable here, adversarially; the transactional path (append_event
itself, which needs a live cursor) gets its integration coverage against
the real cluster (tests/INTEGRATION_CHECKLIST.md).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit.custody import (
    EVENT_TYPES,
    compute_entry_hash,
    content_sha256,
    genesis_hash,
    verify_custody_rows,
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


MID = "11111111-1111-1111-1111-111111111111"
MID_B = "22222222-2222-2222-2222-222222222222"
TS = datetime(2026, 7, 23, 12, 0, 0, 123456, tzinfo=timezone.utc)

# --- vocabulary --------------------------------------------------------------

def t_superseded_by_not_in_vocabulary():
    # Deliberate scope decision: no supersede() flow exists in this
    # codebase (see KNOWN_LIMITATIONS.md). Regression guard against it
    # silently reappearing via a copy-paste from MNEME.
    assert "SUPERSEDED_BY" not in EVENT_TYPES

def t_state_changed_reserved():
    assert "STATE_CHANGED" in EVENT_TYPES

def t_core_vocabulary_present():
    for et in ("STORED", "REINFORCED", "CONTRADICTED_BY", "QUARANTINED",
               "TAINT_FLAGGED", "REHABILITATED"):
        assert et in EVENT_TYPES

# --- genesis_hash --------------------------------------------------------------

def t_genesis_binds_to_memory_id():
    assert genesis_hash(MID) != genesis_hash(MID_B)

def t_genesis_deterministic():
    assert genesis_hash(MID) == genesis_hash(MID)

def t_genesis_documented_value():
    import hashlib
    expected = hashlib.sha256(b"MNEME_CUSTODY_GENESIS:" + MID.encode("utf-8")).hexdigest()
    assert genesis_hash(MID) == expected

def t_genesis_rejects_empty():
    expect_raises(ValueError, lambda: genesis_hash(""))

# --- content_sha256 ------------------------------------------------------------

def t_content_sha256_deterministic():
    assert content_sha256("hello") == content_sha256("hello")

def t_content_sha256_sensitive():
    assert content_sha256("hello") != content_sha256("hello ")

def t_content_sha256_rejects_non_str():
    expect_raises(TypeError, lambda: content_sha256(b"hello"))

# --- compute_entry_hash --------------------------------------------------------

def _hash(**over):
    kw = dict(memory_id=MID, seq=0, prev_hash=genesis_hash(MID),
              event_type="STORED", actor_id="agent-0", reason="test",
              created_at=TS, payload={"content_sha256": "a" * 64})
    kw.update(over)
    return compute_entry_hash(**kw)[0]

def t_hash_deterministic():
    assert _hash() == _hash()

def t_hash_sensitive_to_every_field():
    base = _hash()
    assert _hash(seq=1) != base
    assert _hash(memory_id=MID_B, prev_hash=genesis_hash(MID_B)) != base
    assert _hash(event_type="REINFORCED", payload={}) != base
    assert _hash(actor_id="agent-1") != base
    assert _hash(reason="other") != base
    assert _hash(payload={"content_sha256": "b" * 64}) != base
    assert _hash(prev_hash="0" * 64) != base
    assert _hash(created_at=TS.replace(microsecond=1)) != base

def t_hash_naive_datetime_rejected():
    expect_raises(ValueError, lambda: _hash(created_at=datetime(2026, 1, 1)))

def t_hash_payload_key_order_irrelevant():
    p1 = {"content_sha256": "a" * 64, "x": 1}
    p2 = {"x": 1, "content_sha256": "a" * 64}
    assert _hash(payload=p1) == _hash(payload=p2)

# --- verify_custody_rows -------------------------------------------------------

def _row(seq, event_type, prev_hash, *, actor_id="agent-0", reason="test",
         created_at=None, payload=None):
    payload = payload if payload is not None else ({"content_sha256": "a" * 64} if event_type == "STORED" else {})
    created_at = created_at if created_at is not None else TS.replace(microsecond=seq)
    entry_hash, payload_json = compute_entry_hash(
        memory_id=MID, seq=seq, prev_hash=prev_hash, event_type=event_type,
        actor_id=actor_id, reason=reason, created_at=created_at, payload=payload,
    )
    return {
        "memory_id": MID, "seq": seq, "prev_hash": prev_hash,
        "entry_hash": entry_hash, "event_type": event_type,
        "actor_id": actor_id, "reason": reason, "payload_json": payload_json,
        "created_at": created_at,
    }

def _valid_chain():
    r0 = _row(0, "STORED", genesis_hash(MID))
    r1 = _row(1, "REINFORCED", r0["entry_hash"])
    return [r0, r1]

def t_verify_valid_chain_ok():
    ok, errors = verify_custody_rows(MID, _valid_chain())
    assert ok, errors
    assert errors == []

def t_verify_empty_chain_fails():
    ok, errors = verify_custody_rows(MID, [])
    assert not ok
    assert "empty" in errors[0]

def t_verify_genesis_mismatch_fails():
    rows = _valid_chain()
    rows[0]["prev_hash"] = "f" * 64
    ok, errors = verify_custody_rows(MID, rows)
    assert not ok
    assert "does not link" in errors[0]

def t_verify_seq_gap_fails():
    rows = _valid_chain()
    rows[1]["seq"] = 5
    ok, errors = verify_custody_rows(MID, rows)
    assert not ok
    assert "not dense" in errors[0]

def t_verify_linkage_break_fails():
    rows = _valid_chain()
    rows[1]["prev_hash"] = "e" * 64
    ok, errors = verify_custody_rows(MID, rows)
    assert not ok
    assert "does not link" in errors[0]

def t_verify_tampered_payload_fails():
    rows = _valid_chain()
    rows[1]["payload_json"] = '{"forged":true}'
    ok, errors = verify_custody_rows(MID, rows)
    assert not ok
    assert "tampered" in errors[0]

def t_verify_unknown_event_type_fails():
    rows = _valid_chain()
    rows[1]["event_type"] = "MADE_UP"
    ok, errors = verify_custody_rows(MID, rows)
    assert not ok
    assert "unknown event_type" in errors[0]

def t_verify_non_stored_first_fails():
    r0 = _row(0, "REINFORCED", genesis_hash(MID))
    ok, errors = verify_custody_rows(MID, [r0])
    assert not ok
    assert "does not begin with STORED" in errors[0]

def t_verify_double_stored_fails():
    r0 = _row(0, "STORED", genesis_hash(MID))
    r1 = _row(1, "STORED", r0["entry_hash"])
    ok, errors = verify_custody_rows(MID, [r0, r1])
    assert not ok
    assert "duplicated genesis" in errors[0]

def t_verify_backward_time_fails():
    r0 = _row(0, "STORED", genesis_hash(MID), created_at=TS)
    r1 = _row(1, "REINFORCED", r0["entry_hash"], created_at=TS.replace(microsecond=0) if TS.microsecond else TS)
    # force strictly earlier timestamp than r0
    from datetime import timedelta
    r1["created_at"] = TS - timedelta(seconds=1)
    entry_hash, payload_json = compute_entry_hash(
        memory_id=MID, seq=1, prev_hash=r0["entry_hash"], event_type="REINFORCED",
        actor_id="agent-0", reason="test", created_at=r1["created_at"], payload={},
    )
    r1["entry_hash"] = entry_hash
    r1["payload_json"] = payload_json
    ok, errors = verify_custody_rows(MID, [r0, r1])
    assert not ok
    assert "backwards in time" in errors[0]

def t_verify_grafted_chain_fails():
    # A chain internally consistent for MID_B, presented as MID's.
    r0 = _row(0, "STORED", genesis_hash(MID_B))
    ok, errors = verify_custody_rows(MID, [r0])
    assert not ok
    assert "does not link" in errors[0]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
