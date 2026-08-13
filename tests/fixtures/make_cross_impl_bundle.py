"""
Generate the cross-implementation fixture.

    python tests/fixtures/make_cross_impl_bundle.py

The fixture is a bundle sealed by the PYTHON canonicalizer and carrying content
chosen to break a careless one: accents, CJK, a quote, a backslash, a control
character, and an astral-plane emoji (the one case where Python's codepoint
ordering and JavaScript's UTF-16 ordering could disagree).

Loading it in demo/console.html and seeing B1 pass is the cross-implementation
check: B1 recomputes the seal over the whole document, so a single byte of
disagreement between the two canonicalizers fails it.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from audit.bundle import BUNDLE_VERSION, GENERATOR, SEAL_KEY, seal_of, verify_bundle
from audit.chain import GENESIS_PREV_HASH, compute_entry_hash
from audit.merkle import merkle_root_over_heads

OUT = pathlib.Path(__file__).resolve().parent / "cross_impl.bundle.json"
T0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)

HARD = [
    'Caramelizar cebollas lleva cuarenta minutos — no cinco.',
    'A "covering index" answers the query without touching C:\\base\\table.',
    '向量索引让相似度搜索变得便宜。',
    'Consensus keeps replicas agreeing 🛰️ across failures.',
    'A tab\tand a newline\nsurvive canonicalization or nothing does.',
]


def chain(node_id, events):
    out, prev = [], GENESIS_PREV_HASH
    for i, (event_type, reason, payload, when) in enumerate(events):
        entry_hash, _ = compute_entry_hash(
            node_id=node_id, seq=i, prev_hash=prev, event_type=event_type,
            reason=reason, created_at=when, payload=payload,
        )
        out.append({
            "seq": i, "prev_hash": prev, "entry_hash": entry_hash,
            "event_type": event_type, "reason": reason,
            "created_at": when.isoformat(timespec="microseconds"),
            "payload": payload,
        })
        prev = entry_hash
    return out


def main() -> int:
    memories, seed_events = [], []
    for i, content in enumerate(HARD):
        mid = f"m{i+1}"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        seed_events.append((
            "MEMORY_STORED", f"fixture seed — {content[:18]}…",
            {"memory_id": mid, "region_id": "region-hard", "provider_id": "fixture",
             "content_sha256": digest, "confidence": "0.5000000000"},
            T0 + timedelta(seconds=i),
        ))
        memories.append({
            "memory_id": mid, "region_id": "region-hard", "content": content,
            "content_sha256": digest, "state": "NEUTRAL", "tier": "SHORT_TERM",
            "confidence": "0.5000000000", "custody_status": "CLEAN",
        })

    chains = {"fixture-seeder": chain("fixture-seeder", seed_events)}
    heads = {n: c[-1]["entry_hash"] for n, c in chains.items()}
    body = {
        "bundle_version": BUNDLE_VERSION,
        "generator": GENERATOR,
        "note": "cross-implementation fixture · sealed by the Python canonicalizer",
        "regions": [{"region_id": "region-hard", "locality": "fixture",
                     "status": "ACTIVE", "generation": 1}],
        "memories": memories,
        "signals": [],
        "chains": chains,
        "snapshots": [{
            "snapshot_seq": 0, "node_chain_heads": heads,
            "merkle_root": merkle_root_over_heads(heads),
            "ledger_hash": "fixture",
            "created_at": (T0 + timedelta(seconds=60)).isoformat(timespec="microseconds"),
        }],
    }
    body[SEAL_KEY] = seal_of(body)

    result = verify_bundle(body)
    if not result.ok:
        print("refusing to write a fixture that does not verify:", file=sys.stderr)
        for c in result.failed():
            print(f"  {c.id} {c.detail}", file=sys.stderr)
        return 1

    OUT.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")), encoding="utf-8")
    print(f"[fixture] {OUT.relative_to(ROOT)} — seal {body[SEAL_KEY]}")
    print("[fixture] verifies in Python; open it in demo/console.html and B1 must pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
