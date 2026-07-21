"""Real CockroachDB regression for the Lambda deployment-identity boundary.

The important claim is about ``current_user``. A fake cursor cannot establish
that a Lambda's DSN service account matches its declared STIGMERGY_NODE_ID, so
this test creates disposable database principals on the temporary cluster.
Run via tools/run_authority_integration.sh.
"""

from __future__ import annotations

import os
import threading

import psycopg

from lambdas.common import require_deployment_authority
from audit.chain import run_in_transaction
from ops.authority import (
    NodePrincipalMismatch,
    NodeRevoked,
    RegionCapabilityDenied,
    require_region_capability,
)


def expect(exc_type, fn) -> None:
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:
        raise AssertionError(
            f"expected {exc_type.__name__}, got {type(exc).__name__}: {exc}"
        )
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


def as_principal(root_dsn: str, principal: str):
    # The disposable runner uses an insecure local CockroachDB endpoint. This
    # is test plumbing only; production DSNs come from separate secret values.
    return psycopg.connect(root_dsn.replace("root@", f"{principal}@"), autocommit=False)


def main() -> int:
    root_dsn = os.environ.get("STIGMERGY_TEST_DSN")
    if not root_dsn:
        raise RuntimeError("STIGMERGY_TEST_DSN is required; use tools/run_authority_integration.sh")

    root = psycopg.connect(root_dsn, autocommit=False)
    resolver = sweeper = observer = writer = None
    try:
        with root.cursor() as cur:
            cur.execute("CREATE USER lambda_resolver")
            cur.execute("CREATE USER lambda_sweeper")
            cur.execute("CREATE USER lambda_observer")
            cur.execute("CREATE USER lambda_writer")
            cur.execute("GRANT USAGE ON SCHEMA public TO lambda_resolver, lambda_sweeper, lambda_observer, lambda_writer")
            cur.execute("GRANT SELECT, UPDATE ON TABLE agent_nodes, node_capabilities, node_region_capabilities TO lambda_resolver, lambda_sweeper, lambda_observer, lambda_writer")
            cur.execute(
                """INSERT INTO agent_nodes (node_id, db_principal) VALUES
                   ('lambda-resolver', 'lambda_resolver'),
                   ('lambda-sweeper', 'lambda_sweeper'),
                   ('lambda-observer', 'lambda_observer'),
                   ('lambda-writer', 'lambda_writer')"""
            )
            cur.execute(
                "INSERT INTO node_capabilities (node_id, capability) VALUES "
                "('lambda-sweeper', 'MAINTAIN')"
            )
            cur.execute(
                "INSERT INTO memory_regions (region_id, locality) "
                "VALUES ('lambda-race-region', 'integration-test')"
            )
            cur.execute(
                "INSERT INTO node_region_capabilities (node_id, region_id, capability) "
                "VALUES ('lambda-writer', 'lambda-race-region', 'STORE')"
            )
            cur.execute(
                "CREATE TABLE lambda_authority_commit_probe "
                "(id INT PRIMARY KEY, value INT NOT NULL)"
            )
            cur.execute("INSERT INTO lambda_authority_commit_probe VALUES (1, 0)")
            cur.execute("GRANT SELECT, UPDATE ON TABLE lambda_authority_commit_probe TO lambda_writer")
        root.commit()

        resolver = as_principal(root_dsn, "lambda_resolver")
        sweeper = as_principal(root_dsn, "lambda_sweeper")
        observer = as_principal(root_dsn, "lambda_observer")
        writer = as_principal(root_dsn, "lambda_writer")

        identity = require_deployment_authority(resolver, "lambda-resolver")
        assert identity.node_id == "lambda-resolver"
        expect(
            NodePrincipalMismatch,
            lambda: require_deployment_authority(sweeper, "lambda-resolver"),
        )
        identity = require_deployment_authority(
            sweeper, "lambda-sweeper", capability="MAINTAIN"
        )
        assert identity.node_id == "lambda-sweeper"
        expect(
            RegionCapabilityDenied,
            lambda: require_deployment_authority(
                observer, "lambda-observer", capability="MAINTAIN"
            ),
        )

        # The authority lock makes the serial order explicit. A writer that
        # already proved ACTIVE may complete; revocation waits, then commits.
        # Any operation started after that revocation sees REVOKED. This is
        # stronger than a plain serializable read, which was experimentally
        # falsified: it allowed stale authority + unrelated write to commit.
        checked = threading.Event()
        release_writer = threading.Event()
        writer_result: list[BaseException | str] = []
        revoker_result: list[BaseException | str] = []
        first_attempt = [True]

        def writer_operation(cur):
            require_region_capability(
                cur, node_id="lambda-writer", region_id="lambda-race-region", capability="STORE"
            )
            if first_attempt[0]:
                first_attempt[0] = False
                checked.set()
                assert release_writer.wait(timeout=10), "test did not release writer"
            cur.execute("UPDATE lambda_authority_commit_probe SET value = value + 1 WHERE id = 1")
            return "committed"

        def run_writer():
            try:
                writer_result.append(run_in_transaction(writer, writer_operation))
            except BaseException as exc:
                writer_result.append(exc)

        thread = threading.Thread(target=run_writer)
        thread.start()
        assert checked.wait(timeout=10), "writer did not complete authority read"

        def run_revoker():
            try:
                with root.cursor() as cur:
                    cur.execute(
                        "UPDATE agent_nodes SET status='REVOKED', revoked_at=now(), revoke_reason='test race' "
                        "WHERE node_id='lambda-writer'"
                    )
                root.commit()
                revoker_result.append("revoked")
            except BaseException as exc:
                root.rollback()
                revoker_result.append(exc)

        revoker = threading.Thread(target=run_revoker)
        revoker.start()
        release_writer.set()
        thread.join(timeout=15)
        revoker.join(timeout=15)
        assert not thread.is_alive(), "writer did not finish"
        assert not revoker.is_alive(), "revoker did not finish"
        assert writer_result == ["committed"], writer_result
        assert revoker_result == ["revoked"], revoker_result
        with root.cursor() as cur:
            cur.execute("SELECT value FROM lambda_authority_commit_probe WHERE id = 1")
            assert cur.fetchone()[0] == 1, "pre-revocation write did not commit"
        expect(
            NodeRevoked,
            lambda: run_in_transaction(
                writer,
                lambda cur: require_region_capability(
                    cur, node_id="lambda-writer", region_id="lambda-race-region", capability="STORE"
                ),
            ),
        )
        print("lambda authority integration: PASS (principal binding, MAINTAIN fail-closed, revoke ordering)")
        return 0
    finally:
        for conn in (resolver, sweeper, observer, writer, root):
            if conn is not None:
                conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
