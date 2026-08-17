"""Finish the production authority bootstrap for the CockroachDB Cloud deploy.

The out-of-band bootstrap (see AUTHORITY_MODEL.md) and Track A infra bootstrap
already registered ``authority-controller`` (AUTHORITY_ADMIN + REGION_ADMIN),
``resolver-prod``, and ``sweeper-prod`` (MAINTAIN). This script completes the
remaining audited authority state the deployment contract requires:

  1. register ``seeder-prod`` (principal ``stigmergy_seeder``)
  2. grant it global REGION_ADMIN (it creates regions and seeds the corpus)
  3. create one region per demo corpus theme
  4. grant seeder regional STORE and resolver-prod regional RESOLVE per region

Every mutation goes through the audited ``ops.authority`` / ``ops.regions``
functions, so each seals a hash-chained audit event (Invariant 3). Raw SQL is
deliberately NOT used here.

Run as the authority-controller principal: CockroachDB ``current_user`` must
equal ``ops_admin`` or ``require_authority_administrator`` rejects the
transaction. The DSN must point at the ``stigmergy`` database, not defaultdb.

Idempotent: each already-existing node/region is skipped in its own
transaction (an INSERT conflict aborts only its transaction), and the grant
helpers are internal no-ops when the grant is already ACTIVE.
"""
from __future__ import annotations

import argparse

import psycopg

from audit.chain import run_in_transaction
from ops.authority import (
    NodeRegistrationConflict,
    grant_node_capability,
    grant_region_capability,
    register_node,
)
from ops.regions import RegionExists, create_region
from demo.corpus import all_regions

EXECUTOR = "authority-controller"
SEEDER = "seeder-prod"
SEEDER_PRINCIPAL = "stigmergy_seeder"
RESOLVER = "resolver-prod"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True, help="DSN for the ops_admin principal, pointing at the stigmergy database")
    ap.add_argument("--locality", default="aws-us-east-1", help="physical placement label for memory_regions.locality")
    args = ap.parse_args()

    conn = psycopg.connect(args.dsn, autocommit=False)
    try:
        # 1. seeder-prod node (own transaction: a conflict aborts only this one)
        try:
            run_in_transaction(conn, lambda cur: register_node(
                cur, executor_node_id=EXECUTOR, node_id=SEEDER,
                db_principal=SEEDER_PRINCIPAL,
                reason="Track A infra bootstrap: register seeding/migration node",
            ))
            print(f"registered {SEEDER}")
        except NodeRegistrationConflict:
            print(f"{SEEDER} already registered — skipping")

        # 2. seeder global REGION_ADMIN (internal no-op if already active)
        run_in_transaction(conn, lambda cur: grant_node_capability(
            cur, executor_node_id=EXECUTOR, node_id=SEEDER, capability="REGION_ADMIN",
            reason="seeder creates regions and seeds the demo corpus",
        ))
        print(f"granted {SEEDER} REGION_ADMIN")

        # 3. one region per corpus theme (own transaction each)
        for region in all_regions():
            try:
                run_in_transaction(conn, lambda cur, r=region: create_region(
                    cur, node_id=EXECUTOR, region_id=r, locality=args.locality,
                    reason="Track A infra bootstrap: create demo corpus region",
                ))
                print(f"created region {region}")
            except RegionExists:
                print(f"region {region} already exists — skipping")

        # 4. regional grants: seeder STORE, resolver-prod RESOLVE
        for region in all_regions():
            run_in_transaction(conn, lambda cur, r=region: grant_region_capability(
                cur, executor_node_id=EXECUTOR, node_id=SEEDER, region_id=r,
                capability="STORE", reason="seeder stores seeded memories in region",
            ))
            run_in_transaction(conn, lambda cur, r=region: grant_region_capability(
                cur, executor_node_id=EXECUTOR, node_id=RESOLVER, region_id=r,
                capability="RESOLVE", reason="resolver resolves recruitment in region",
            ))
            print(f"granted STORE({SEEDER}) + RESOLVE({RESOLVER}) on {region}")

        print("prod authority bootstrap: complete")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
