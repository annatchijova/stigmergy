"""
STIGMERGY — Lambda 2 of 2: cron-driven sweeper.

EventBridge schedule -> this function -> two bounded drains:

    1. expire_signals   (PENDING past expires_at  -> EXPIRED)
    2. sweep_orphans    (stale memories           -> ORPHANED)

Deliberately cron, not changefeed (ARCHITECTURE.md gives the two
Lambdas different patterns on purpose): expiry and orphaning are
functions of TIME PASSING, not of rows changing — there is no event to
react to when nothing happens, and nothing happening is exactly when
these fire.

Budget discipline: a Lambda invocation has a time limit, and a backlog
after an outage can exceed it. Each drain runs at most MAX_CHUNKS
transactions of CHUNK_SIZE rows (bounded_drain) and REPORTS whether it
finished — `drained: false` in the summary means work remains and the
next tick continues. Explicit degradation over a timeout loop.

Configuration (env):
    STIGMERGY_NODE_ID          required — this function's node identity
    STIGMERGY_DSN              required — CockroachDB connection string
    SWEEP_CHUNK_SIZE           rows per transaction      (default 1000)
    SWEEP_MAX_CHUNKS           transactions per drain    (default 20)
    ORPHAN_WINDOW_SECONDS      staleness window          (default 604800 = 7d)
"""

from __future__ import annotations

import json
from datetime import timedelta

from audit.chain import run_in_transaction
from ops.orphans import sweep_orphans
from ops.recruitment import expire_signals
from .common import (
    bounded_drain,
    env_int,
    get_connection,
    require_deployment_authority,
    require_node_id,
)


def handler(event=None, context=None):
    node_id = require_node_id()
    chunk_size = env_int("SWEEP_CHUNK_SIZE", 1000)
    max_chunks = env_int("SWEEP_MAX_CHUNKS", 20)
    window = timedelta(seconds=env_int("ORPHAN_WINDOW_SECONDS", 7 * 24 * 3600))
    conn = get_connection()
    # Do not report a successful empty sweep unless this deployment's DB
    # credential owns the node and has the one global maintenance authority.
    require_deployment_authority(conn, node_id, capability="MAINTAIN")

    signals = bounded_drain(
        lambda: run_in_transaction(
            conn,
            lambda cur: expire_signals(
                cur, node_id=node_id, limit=chunk_size,
            ),
        ),
        max_chunks=max_chunks,
        chunk_size=chunk_size,
    )

    orphans = bounded_drain(
        lambda: len(run_in_transaction(
            conn,
            lambda cur: sweep_orphans(
                cur, node_id=node_id, window=window, limit=chunk_size,
            ),
        )),
        max_chunks=max_chunks,
        chunk_size=chunk_size,
    )

    body = {
        "signals_expired": {"total": signals.total,
                            "chunks": signals.chunks_run,
                            "drained": signals.drained},
        "memories_orphaned": {"total": orphans.total,
                              "chunks": orphans.chunks_run,
                              "drained": orphans.drained},
        "window_seconds": int(window.total_seconds()),
        "chunk_size": chunk_size,
    }
    # Counts only: CloudWatch receives an operational index without memory
    # content, identifiers, credentials, or the DSN entering the log stream.
    print(json.dumps({"event": "SWEEP_COMPLETED", **body}, sort_keys=True))
    return {"statusCode": 200, "body": json.dumps(body)}
