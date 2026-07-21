"""Real CockroachDB authority regression; run via tools/run_authority_integration.sh.

This intentionally does not use a mock cursor: identity derives from the
database's current_user, so a mock would not test the security boundary.
The runner supplies an empty temporary database with schema.sql already applied.
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction

import psycopg

from audit.chain import run_in_transaction, verify_chain
from embeddings.deterministic import DeterministicEmbeddingProvider
from ops.authority import NodePrincipalMismatch, NodeRevoked, require_region_capability
from ops.controller import AgentOwnershipDenied, observe
from ops.memories import recall, store
from ops.regions import create_region


def expect(exc_type, fn) -> None:
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:
        raise AssertionError(f"expected {exc_type.__name__}, got {type(exc).__name__}: {exc}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


def main() -> int:
    dsn = os.environ.get("STIGMERGY_TEST_DSN")
    if not dsn:
        raise RuntimeError("STIGMERGY_TEST_DSN is required; use tools/run_authority_integration.sh")
    conn = psycopg.connect(dsn, autocommit=False)
    provider = DeterministicEmbeddingProvider()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO authority_administrators (db_principal) VALUES ('root')")
            cur.execute("INSERT INTO agent_nodes (node_id, db_principal) VALUES ('root-node', 'root'), ('foreign-node', 'foreign')")
            cur.execute("INSERT INTO node_capabilities (node_id, capability) VALUES ('root-node', 'REGION_ADMIN'), ('root-node', 'RECALL_GLOBAL')")
        conn.commit()
        run_in_transaction(conn, lambda cur: create_region(cur, node_id='root-node', region_id='r1', locality='test', reason='authority integration'))
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO node_region_capabilities (node_id, region_id, capability)
                           VALUES ('root-node', 'r1', 'STORE'), ('root-node', 'r1', 'OBSERVE')""")
            cur.execute("""INSERT INTO agent_search_state
                           (agent_id, owner_node_id, mode, confidence_ema, ema_prev, stagnation_count)
                           VALUES ('foreign-agent', 'foreign-node', 'ROAMING', 0, 0, 0)""")
        conn.commit()
        stored = run_in_transaction(conn, lambda cur: store(cur, provider=provider, node_id='root-node', region_id='r1', content='authority integration memory', reason='seed'))
        expect(NodePrincipalMismatch, lambda: run_in_transaction(conn, lambda cur: require_region_capability(cur, node_id='foreign-node', region_id='r1', capability='STORE')))
        expect(AgentOwnershipDenied, lambda: run_in_transaction(conn, lambda cur: observe(cur, node_id='root-node', agent_id='foreign-agent', observation=Fraction(1, 2))))
        with conn.cursor() as cur:
            cur.execute("UPDATE agent_nodes SET status='REVOKED', revoked_at=now(), revoke_reason='test' WHERE node_id='root-node'")
        conn.commit()
        expect(NodeRevoked, lambda: run_in_transaction(conn, lambda cur: store(cur, provider=provider, node_id='root-node', region_id='r1', content='blocked', reason='revoked')))
        expect(NodeRevoked, lambda: run_in_transaction(conn, lambda cur: recall(cur, provider=provider, node_id='root-node', query_text='blocked', region_id=None)))
        with conn.cursor() as cur:
            chain = verify_chain(cur, 'root-node')
        assert chain.ok, chain.detail
        print('authority integration: PASS (impersonation, ownership, revocation, chain)')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
