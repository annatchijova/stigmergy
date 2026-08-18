"""Bootstrap audited regions and grants for the disposable secure demo."""
from __future__ import annotations
import argparse
import psycopg

from audit.chain import run_in_transaction
from ops.authority import grant_node_capability, grant_region_capability
from ops.regions import RegionExists, create_region
from demo.corpus import all_regions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--agents", type=int, required=True)
    args = ap.parse_args()
    conn = psycopg.connect(args.dsn, autocommit=False)
    try:
        def bootstrap(cur):
            for region in all_regions():
                try:
                    create_region(cur, node_id="demo-seeder", region_id=region,
                                  locality="local-demo", reason="secure demo bootstrap")
                except RegionExists:
                    pass
            for region in all_regions():
                grant_region_capability(cur, executor_node_id="demo-seeder",
                                        node_id="demo-seeder", region_id=region,
                                        capability="STORE", reason="secure demo seed grant")
                for i in range(args.agents):
                    for capability in ("OBSERVE", "REINFORCE", "SIGNAL"):
                        grant_region_capability(cur, executor_node_id="demo-seeder",
                                                node_id=f"agent-{i}", region_id=region,
                                                capability=capability,
                                                reason="secure demo agent grant")
                grant_region_capability(cur, executor_node_id="demo-seeder",
                                        node_id="demo-local-resolver", region_id=region,
                                        capability="RESOLVE", reason="secure demo resolver grant")
            for i in range(args.agents):
                grant_node_capability(cur, executor_node_id="demo-seeder",
                                      node_id=f"agent-{i}", capability="RECALL_GLOBAL",
                                      reason="secure demo roaming grant")
            # The local resolver also stands in for the cron sweeper
            # (expire_signals), which requires the global MAINTAIN capability.
            grant_node_capability(cur, executor_node_id="demo-seeder",
                                  node_id="demo-local-resolver", capability="MAINTAIN",
                                  reason="secure demo sweeper grant")
        run_in_transaction(conn, bootstrap)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
