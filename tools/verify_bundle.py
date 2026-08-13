"""
Verify a sealed evidence bundle. Takes the file and nothing else.

    python -m tools.verify_bundle run.bundle.json

No database, no network, no trust in whoever produced the file. Exits 0 when
every check passes and 1 when any fails, so it drops into CI unchanged.

demo/console.html runs the same six checks in JavaScript. Two independent
implementations of one claim is the point: if they ever disagree about a
bundle, at least one of them is wrong, and that is worth knowing.

Read audit/bundle.py for what a bundle does NOT prove — authority, wholesale
omission without a published root, and the per-memory custody chains are all
outside these six checks.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from audit.bundle import SEAL_KEY, verify_bundle


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a STIGMERGY evidence bundle.")
    ap.add_argument("bundle", help="path to a bundle JSON file")
    ap.add_argument("--quiet", action="store_true", help="print only the verdict")
    args = ap.parse_args()

    path = pathlib.Path(args.bundle)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"no such bundle: {path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"{path} is not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print(f"{path} is not a bundle object.", file=sys.stderr)
        return 2

    result = verify_bundle(data)

    if not args.quiet:
        print(f"bundle    {path}")
        print(f"version   {data.get('bundle_version')}   generator {data.get('generator')!r}")
        print(f"seal      {data.get(SEAL_KEY)}")
        nodes = data.get("chains") or {}
        entries = sum(len(v) for v in nodes.values())
        print(f"contents  {len(data.get('memories') or [])} memories · "
              f"{len(nodes)} node chains · {entries} entries · "
              f"{len(data.get('snapshots') or [])} snapshots\n")
        for c in result.checks:
            mark = "PASS" if c.ok else "FAIL"
            print(f"  {c.id}  {mark}  {c.claim}")
            if not c.ok:
                print(f"          {c.detail}")
        print()

    if result.ok:
        print("VERIFIED: every check passed.")
        return 0
    print(f"FAILED: {len(result.failed())} check(s) did not pass "
          f"({', '.join(c.id for c in result.failed())}).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
