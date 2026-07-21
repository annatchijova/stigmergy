"""
Pure-function tests for the Lambda layer — envelope parsing, the bounded
drain loop, and configuration boundaries. No database, no psycopg: the
handlers import their heavy dependency lazily, and THAT is itself under
test here (this suite runs in an environment where psycopg is absent).
"""

from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lambdas.common as common
from lambdas.common import (
    DrainSummary,
    bounded_drain,
    env_int,
    require_deployment_authority,
    require_node_id,
)
import lambdas.changefeed_resolver as resolver_lambda
import lambdas.cron_sweeper as sweeper_lambda
from lambdas.changefeed_resolver import ParsedBatch, parse_changefeed_envelope

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

def entry(memory_id="m1", origin="r1", status="PENDING", **over):
    after = {"memory_id": memory_id, "origin_region": origin, "status": status}
    after.update(over)
    return {"after": after, "key": [memory_id], "updated": "t"}

# --- envelope parsing ----------------------------------------------------------

def t_single_pending_signal_becomes_one_attempt():
    p = parse_changefeed_envelope({"payload": [entry()], "length": 1})
    assert len(p.attempts) == 1
    assert p.attempts[0].memory_id == "m1"
    assert p.attempts[0].target_region == "r1"
    assert p.malformed == [] and p.ignored == 0

def t_non_pending_rows_are_ignored_not_malformed():
    # our own resolutions/sweeps echo back through the feed
    p = parse_changefeed_envelope({"payload": [
        entry(status="ACCEPTED"), entry(status="EXPIRED"), entry(status="REJECTED"),
    ]})
    assert p.attempts == [] and p.malformed == [] and p.ignored == 3

def t_tombstones_are_ignored():
    p = parse_changefeed_envelope({"payload": [{"after": None, "key": ["m1"]}]})
    assert p.attempts == [] and p.ignored == 1 and p.malformed == []

def t_heartbeat_envelope_is_ignored():
    p = parse_changefeed_envelope({"resolved": "1720000000.0000000000"})
    assert p.attempts == [] and p.ignored == 1 and p.malformed == []

def t_heartbeat_row_inside_payload_is_ignored():
    p = parse_changefeed_envelope({"payload": [{"resolved": "t"}]})
    assert p.attempts == [] and p.ignored == 1 and p.malformed == []

def t_batch_dedupes_same_memory_and_origin():
    p = parse_changefeed_envelope({"payload": [entry(), entry(), entry()]})
    assert len(p.attempts) == 1 and p.ignored == 2

def t_same_memory_different_origins_are_distinct_attempts():
    p = parse_changefeed_envelope({"payload": [entry(origin="r1"), entry(origin="r2")]})
    assert len(p.attempts) == 2
    assert [a.target_region for a in p.attempts] == ["r1", "r2"]  # first-seen order

def t_missing_memory_id_is_named_malformed():
    bad = {"after": {"origin_region": "r1", "status": "PENDING"}}
    p = parse_changefeed_envelope({"payload": [bad]})
    assert p.attempts == [] and len(p.malformed) == 1
    assert "memory_id" in p.malformed[0]

def t_missing_origin_is_named_malformed():
    bad = {"after": {"memory_id": "m1", "status": "PENDING"}}
    p = parse_changefeed_envelope({"payload": [bad]})
    assert len(p.malformed) == 1 and "origin_region" in p.malformed[0]

def t_one_bad_entry_does_not_poison_its_neighbors():
    p = parse_changefeed_envelope({"payload": [
        entry("m1", "r1"), "garbage", entry("m2", "r2"),
    ]})
    assert len(p.attempts) == 2 and len(p.malformed) == 1

def t_non_dict_envelope_is_malformed():
    p = parse_changefeed_envelope("not json object")
    assert p.attempts == [] and len(p.malformed) == 1

def t_payload_missing_is_malformed():
    p = parse_changefeed_envelope({"length": 3})
    assert len(p.malformed) == 1 and "payload" in p.malformed[0]

def t_empty_or_whitespace_ids_are_malformed():
    p = parse_changefeed_envelope({"payload": [entry(memory_id="  "), entry(origin="")]})
    assert p.attempts == [] and len(p.malformed) == 2

# --- bounded_drain ----------------------------------------------------------------

def make_step(counts):
    it = iter(counts)
    return lambda: next(it)

def t_drain_stops_at_zero():
    s = bounded_drain(make_step([5, 3, 0, 99]), max_chunks=10)
    assert s == DrainSummary(chunks_run=3, total=8, drained=True)

def t_drain_respects_budget_and_reports_leftover():
    s = bounded_drain(make_step([5, 5, 5]), max_chunks=3)
    assert s.chunks_run == 3 and s.total == 15 and s.drained is False

def t_drain_short_chunk_proves_exhaustion():
    # a chunk below chunk_size means the LIMIT was not reached: done,
    # one transaction earlier than waiting for the zero.
    s = bounded_drain(make_step([5, 2]), max_chunks=10, chunk_size=5)
    assert s == DrainSummary(chunks_run=2, total=7, drained=True)

def t_drain_full_chunks_keep_going_without_chunk_size_shortcut():
    s = bounded_drain(make_step([5, 5, 0]), max_chunks=10, chunk_size=5)
    assert s.chunks_run == 3 and s.drained is True

def t_drain_rejects_bad_budget():
    expect_raises(ValueError, lambda: bounded_drain(lambda: 0, max_chunks=0))
    expect_raises(TypeError, lambda: bounded_drain(lambda: 0, max_chunks=True))
    expect_raises(ValueError, lambda: bounded_drain(lambda: 0, max_chunks=5, chunk_size=0))

def t_drain_rejects_non_int_step_result():
    expect_raises(TypeError, lambda: bounded_drain(lambda: "5", max_chunks=3))
    expect_raises(TypeError, lambda: bounded_drain(lambda: True, max_chunks=3))
    expect_raises(ValueError, lambda: bounded_drain(lambda: -1, max_chunks=3))

# --- configuration boundaries --------------------------------------------------------

def t_node_id_required_and_validated():
    old = os.environ.pop("STIGMERGY_NODE_ID", None)
    try:
        expect_raises(RuntimeError, require_node_id)
        os.environ["STIGMERGY_NODE_ID"] = "bad id with spaces"
        expect_raises(RuntimeError, require_node_id)
        os.environ["STIGMERGY_NODE_ID"] = "x" * 65
        expect_raises(RuntimeError, require_node_id)
        os.environ["STIGMERGY_NODE_ID"] = "lambda-resolver-1"
        assert require_node_id() == "lambda-resolver-1"
    finally:
        if old is None:
            os.environ.pop("STIGMERGY_NODE_ID", None)
        else:
            os.environ["STIGMERGY_NODE_ID"] = old

def t_env_int_defaults_and_rejects_garbage():
    os.environ.pop("STIG_TEST_INT", None)
    assert env_int("STIG_TEST_INT", 7) == 7
    os.environ["STIG_TEST_INT"] = "12"
    try:
        assert env_int("STIG_TEST_INT", 7) == 12
        os.environ["STIG_TEST_INT"] = "twelve"
        expect_raises(RuntimeError, lambda: env_int("STIG_TEST_INT", 7))
        os.environ["STIG_TEST_INT"] = "0"
        expect_raises(RuntimeError, lambda: env_int("STIG_TEST_INT", 7))
    finally:
        os.environ.pop("STIG_TEST_INT", None)

def t_deployment_authority_binds_identity_and_optional_global_capability():
    # Unit-test the Lambda boundary without pretending a mock cursor can prove
    # current_user.  The real principal-binding path is exercised against a
    # CockroachDB cluster in the integration checklist.
    old_runner = common.run_in_transaction
    old_active = common.require_active_node
    old_capability = common.require_node_capability
    calls = []
    try:
        common.run_in_transaction = lambda conn, fn: fn("cursor")
        common.require_active_node = lambda cur, *, node_id: calls.append(
            ("active", cur, node_id)
        ) or "identity"
        common.require_node_capability = lambda cur, *, node_id, capability: calls.append(
            ("capability", cur, node_id, capability)
        ) or "capability-identity"
        assert require_deployment_authority("connection", "resolver-node") == "identity"
        assert require_deployment_authority(
            "connection", "sweeper-node", capability="MAINTAIN"
        ) == "capability-identity"
        assert calls == [
            ("active", "cursor", "resolver-node"),
            ("capability", "cursor", "sweeper-node", "MAINTAIN"),
        ]
    finally:
        common.run_in_transaction = old_runner
        common.require_active_node = old_active
        common.require_node_capability = old_capability

def t_resolver_heartbeat_validates_deployment_identity():
    old_node = os.environ.get("STIGMERGY_NODE_ID")
    old_connection = resolver_lambda.get_connection
    old_authority = resolver_lambda.require_deployment_authority
    calls = []
    try:
        os.environ["STIGMERGY_NODE_ID"] = "resolver-node"
        resolver_lambda.get_connection = lambda: "resolver-connection"
        resolver_lambda.require_deployment_authority = lambda conn, node: calls.append((conn, node))
        result = resolver_lambda.handler({"resolved": "1700000000.0"})
        assert result["statusCode"] == 200
        assert calls == [("resolver-connection", "resolver-node")]
    finally:
        resolver_lambda.get_connection = old_connection
        resolver_lambda.require_deployment_authority = old_authority
        if old_node is None:
            os.environ.pop("STIGMERGY_NODE_ID", None)
        else:
            os.environ["STIGMERGY_NODE_ID"] = old_node

def t_sweeper_requires_maintain_before_empty_drain():
    old_node = os.environ.get("STIGMERGY_NODE_ID")
    old_connection = sweeper_lambda.get_connection
    old_authority = sweeper_lambda.require_deployment_authority
    old_drain = sweeper_lambda.bounded_drain
    calls = []
    try:
        os.environ["STIGMERGY_NODE_ID"] = "sweeper-node"
        sweeper_lambda.get_connection = lambda: "sweeper-connection"
        sweeper_lambda.require_deployment_authority = lambda conn, node, *, capability=None: calls.append(
            (conn, node, capability)
        )
        sweeper_lambda.bounded_drain = lambda *args, **kwargs: DrainSummary(1, 0, True)
        result = sweeper_lambda.handler()
        assert result["statusCode"] == 200
        assert calls == [("sweeper-connection", "sweeper-node", "MAINTAIN")]
    finally:
        sweeper_lambda.get_connection = old_connection
        sweeper_lambda.require_deployment_authority = old_authority
        sweeper_lambda.bounded_drain = old_drain
        if old_node is None:
            os.environ.pop("STIGMERGY_NODE_ID", None)
        else:
            os.environ["STIGMERGY_NODE_ID"] = old_node

def t_handlers_importable_without_psycopg():
    # the lazy-import discipline, pinned: importing the handler modules
    # must never require the driver (this environment does not have it).
    try:
        import psycopg  # noqa: F401
        return  # driver present here; the pin is vacuous but not wrong
    except ImportError:
        pass
    import lambdas.changefeed_resolver  # noqa: F401
    import lambdas.cron_sweeper  # noqa: F401


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
