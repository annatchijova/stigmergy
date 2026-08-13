"""
Pure-function tests for audit/bundle.py — no database required.

The export path needs a cursor and gets its integration coverage from
tests/INTEGRATION_CHECKLIST.md. The VERIFIER is the part that has to be
adversarial, and it is pure by construction: it takes a bundle and nothing
else. So every check gets two tests — one that it passes on honest evidence,
and one that it fails on a specific, named forgery.

A verifier that has never been shown failing is not evidence of anything.
"""

from __future__ import annotations

import copy
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit.bundle import (
    BUNDLE_VERSION, GENERATOR, SEAL_KEY, replay_memory_state, seal_of, verify_bundle,
)
from audit.chain import GENESIS_PREV_HASH, compute_entry_hash
from audit.merkle import merkle_root_over_heads

failures = []
T0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


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


# ------------------------------------------------------------- a fixture --

def _chain(node_id, events):
    """Build a genuine hash chain: every entry sealed the way append_event seals it."""
    out, prev = [], GENESIS_PREV_HASH
    for i, (event_type, reason, payload, when) in enumerate(events):
        entry_hash, _ = compute_entry_hash(
            node_id=node_id, seq=i, prev_hash=prev, event_type=event_type,
            reason=reason, created_at=when, payload=payload,
        )
        out.append({
            "seq": i, "prev_hash": prev, "entry_hash": entry_hash,
            "event_type": event_type, "reason": reason,
            "created_at": when.isoformat(timespec="microseconds"), "payload": payload,
        })
        prev = entry_hash
    return out


CONTENT = "A covering index answers the query without ever reading the base table."
DIGEST = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()


def honest_bundle() -> dict:
    seeder = _chain("seeder", [
        ("MEMORY_STORED", "corpus seed", {
            "memory_id": "m1", "region_id": "region-cooking", "provider_id": "test",
            "content_sha256": DIGEST, "confidence": "0.5000000000",
        }, T0),
    ])
    agent = _chain("agent-0", [
        ("MEMORY_REINFORCED", "top hit", {
            "memory_id": "m1", "old_state": "NEUTRAL", "new_state": "REINFORCED",
            "old_confidence": "0.5000000000", "new_confidence": "0.6000000000",
            "alpha": "1/5",
        }, T0 + timedelta(seconds=10)),
    ])
    resolver = _chain("resolver", [
        ("MEMORY_MIGRATED", "consensus", {
            "memory_id": "m1", "from_region": "region-cooking",
            "target_region": "region-databases", "old_tier": "SHORT_TERM",
            "new_tier": "REGIONAL", "live_signals": 2, "votes_for": 2,
            "vigor_for": "1.2000000000", "vigor_max": "2.0000000000", "quorum": "1/2",
            "accepted_signal_ids": ["s1", "s2"],
        }, T0 + timedelta(seconds=20)),
    ])
    chains = {"seeder": seeder, "agent-0": agent, "resolver": resolver}
    heads = {n: c[-1]["entry_hash"] for n, c in chains.items()}
    body = {
        "bundle_version": BUNDLE_VERSION, "generator": GENERATOR, "note": "",
        "regions": [
            {"region_id": "region-cooking", "locality": "demo", "status": "ACTIVE", "generation": 1},
            {"region_id": "region-databases", "locality": "demo", "status": "ACTIVE", "generation": 1},
        ],
        "memories": [{
            "memory_id": "m1", "region_id": "region-databases", "content": CONTENT,
            "content_sha256": DIGEST, "state": "REINFORCED", "tier": "REGIONAL",
            "confidence": "0.6000000000", "custody_status": "CLEAN",
        }],
        "signals": [],
        "chains": chains,
        "snapshots": [{
            "snapshot_seq": 0, "node_chain_heads": heads,
            "merkle_root": merkle_root_over_heads(heads),
            "ledger_hash": "unused-by-B6",
            "created_at": (T0 + timedelta(seconds=30)).isoformat(timespec="microseconds"),
        }],
    }
    body[SEAL_KEY] = seal_of(body)
    return body


def _assert_only(bundle, failing_id):
    v = verify_bundle(bundle)
    bad = {c.id for c in v.failed()}
    assert failing_id in bad, f"expected {failing_id} to fail, failures were {bad or 'none'}"
    return v


# -------------------------------------------------------------- the tests --

def t_honest_bundle_passes_every_check():
    v = verify_bundle(honest_bundle())
    assert v.ok, f"honest bundle failed: {[(c.id, c.detail) for c in v.failed()]}"
    assert len(v.checks) == 6, len(v.checks)


def t_b1_reseal_catches_any_edit_anywhere():
    b = honest_bundle()
    b["memories"][0]["custody_status"] = "TAINT_FLAGGED"   # not covered by B5
    _assert_only(b, "B1")


def t_b1_passes_when_the_forger_reseals():
    """A forger who reseals defeats B1 — that is exactly why B1 is not the
    only check. The chain is what catches them."""
    b = honest_bundle()
    b["memories"][0]["state"] = "NEUTRAL"
    b[SEAL_KEY] = seal_of(b)
    v = verify_bundle(b)
    bad = {c.id for c in v.failed()}
    assert bad == {"B5"}, f"expected only B5 to survive the reseal, got {bad}"


def t_b2_catches_a_removed_entry():
    b = honest_bundle()
    seeder = _chain("seeder", [
        ("MEMORY_STORED", "a", {"memory_id": "m1", "region_id": "r", "provider_id": "t",
                                "content_sha256": DIGEST, "confidence": "0.5000000000"}, T0),
        ("REDISCOVERED", "b", {"memory_id": "m1", "provider_id": "t",
                               "previous_tier": "ORPHANED", "new_tier": "SHORT_TERM"},
         T0 + timedelta(seconds=1)),
    ])
    del seeder[0]                      # drop the first entry, renumber nothing
    b["chains"]["seeder"] = seeder
    b[SEAL_KEY] = seal_of(b)
    _assert_only(b, "B2")


def t_b2_catches_a_broken_link():
    b = honest_bundle()
    b["chains"]["seeder"][0]["prev_hash"] = "0" * 64
    b[SEAL_KEY] = seal_of(b)
    _assert_only(b, "B2")


def t_b3_catches_an_edited_payload():
    b = honest_bundle()
    b["chains"]["resolver"][0]["payload"]["votes_for"] = 99
    b[SEAL_KEY] = seal_of(b)
    _assert_only(b, "B3")


def t_b4_catches_rewritten_content():
    b = honest_bundle()
    b["memories"][0]["content"] = CONTENT.replace("without", "while")
    b[SEAL_KEY] = seal_of(b)
    _assert_only(b, "B4")


def t_b5_catches_a_state_the_chain_never_authorized():
    b = honest_bundle()
    b["memories"][0]["region_id"] = "region-cooking"    # never migrated back
    b[SEAL_KEY] = seal_of(b)
    _assert_only(b, "B5")


def t_b5_catches_an_inflated_confidence():
    b = honest_bundle()
    b["memories"][0]["confidence"] = "0.9000000000"
    b[SEAL_KEY] = seal_of(b)
    _assert_only(b, "B5")


def t_b5_refuses_a_memory_with_no_birth_event():
    """Honest degradation: an undeclarable state is a failure, not a pass."""
    b = honest_bundle()
    b["memories"].append({
        "memory_id": "ghost", "region_id": "region-cooking", "content": "x",
        "content_sha256": hashlib.sha256(b"x").hexdigest(), "state": "NEUTRAL",
        "tier": "SHORT_TERM", "confidence": "0.5000000000", "custody_status": "CLEAN",
    })
    b[SEAL_KEY] = seal_of(b)
    v = _assert_only(b, "B5")
    assert "not derivable" in v.checks[4].detail, v.checks[4].detail


def t_b6_catches_a_rewritten_merkle_root():
    b = honest_bundle()
    b["snapshots"][0]["merkle_root"] = "f" * 64
    b[SEAL_KEY] = seal_of(b)
    _assert_only(b, "B6")


def t_b6_catches_a_node_dropped_from_the_bundle():
    """The one defence against wholesale omission: an earlier snapshot still
    commits to the node that is no longer here."""
    b = honest_bundle()
    del b["chains"]["agent-0"]
    b[SEAL_KEY] = seal_of(b)
    v = _assert_only(b, "B6")
    assert "does not contain" in v.checks[5].detail, v.checks[5].detail


def t_replay_is_order_independent_across_nodes():
    """Entries are ordered by timestamp, so shuffling the node dict cannot
    change what the evidence says the state must be."""
    b = honest_bundle()
    forward = replay_memory_state(b["chains"])
    shuffled = {k: b["chains"][k] for k in reversed(list(b["chains"]))}
    assert replay_memory_state(shuffled) == forward


def t_seal_ignores_key_order_but_not_values():
    b = honest_bundle()
    reordered = {k: b[k] for k in reversed(list(b))}
    assert seal_of(reordered) == seal_of(b)
    changed = copy.deepcopy(b)
    changed["note"] = "different"
    assert seal_of(changed) != seal_of(b)


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "cross_impl.bundle.json")

# Pinned so a change in canonicalization cannot slip through as "still passes":
# this seal was produced by audit/canonical.py over content chosen to stress it
# (accents, CJK, a quote, a backslash, a tab, a newline, an astral-plane emoji).
# demo/console.html must recompute the SAME value from the same file — that is
# the cross-implementation check, and B1 is what enforces it.
FIXTURE_SEAL = "2ec62b731d14e6fede0efa513e2a194bee4ba69af6a84c2ca854056fa1e4a366"


def _fixture():
    import json
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def t_cross_impl_fixture_still_seals_to_its_pinned_value():
    b = _fixture()
    assert b[SEAL_KEY] == FIXTURE_SEAL, (
        f"fixture seal drifted: file says {b[SEAL_KEY]}, pinned {FIXTURE_SEAL}")
    assert seal_of(b) == FIXTURE_SEAL, (
        f"canonicalization changed: recomputed {seal_of(b)}, pinned {FIXTURE_SEAL}. "
        "Every previously sealed bundle just stopped verifying — this is a "
        "migration event, not a refactor.")


def t_cross_impl_fixture_verifies():
    v = verify_bundle(_fixture())
    assert v.ok, f"fixture failed: {[(c.id, c.detail) for c in v.failed()]}"


def t_cross_impl_fixture_actually_contains_the_hard_characters():
    """A fixture that quietly lost its awkward content would still pass, and
    would be testing nothing."""
    contents = " ".join(m["content"] for m in _fixture()["memories"])
    for needle, what in (("—", "em dash"), ('"', "quote"), ("\\", "backslash"),
                         ("\t", "tab"), ("\n", "newline"), ("向量", "CJK"),
                         ("\U0001F6F0", "astral-plane emoji")):
        assert needle in contents, f"fixture no longer covers the {what} case"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
