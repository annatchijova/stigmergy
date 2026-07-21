"""
STIGMERGY — Shared Lambda plumbing.

Node identity: a Lambda function IS a node (Invariant 5's per-node
chains include it — the cron sweeper and the changefeed resolver seal
events like any agent process). Its sandboxes are ephemeral, so identity
CANNOT be derived-and-persisted the way a long-lived machine does it:
STIGMERGY_NODE_ID is REQUIRED deployment configuration, validated with
the project's node-id rules, and a missing/invalid value fails the cold
start immediately with our words, not a KeyError three calls deep.

Connection: one lazy module-global psycopg connection, reused across
warm invocations (standard Lambda practice), re-established if closed.
psycopg is imported LAZILY inside get_connection() — the same discipline
as embeddings.minilm: importing this package must never require the
heavy dependency, so the pure tests run anywhere.

bounded_drain: the cron loop as a pure, testable function. A Lambda has
a time budget; draining an unbounded backlog in one invocation is how a
long outage turns into a timeout loop. The drain runs at most max_chunks
steps and REPORTS whether the backlog was exhausted — leftover work is
named, never hidden (Failure philosophy: explicit degradation).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from audit.chain import run_in_transaction
from ops.authority import require_active_node, require_node_capability

_MAX_NODE_ID_LEN = 64
_NODE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

_conn = None  # module-global, reused across warm invocations
_secret_cache: dict[tuple[str, str], str] = {}


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is required deployment configuration and is not set. "
            "Refusing to start with a missing identity or endpoint."
        )
    return value


def require_secret_or_env(
    value_env: str,
    secret_arn_env: str,
    *,
    json_key: str,
) -> str:
    """Read a deployment secret from one deliberate source.

    Local development may supply the value directly. AWS deployments supply a
    Secrets Manager ARN and receive a JSON secret containing ``json_key``.
    Supplying both is a configuration contradiction, not a fallback: it makes
    it unclear which credential the function is actually using. Values are
    cached only in the warm Lambda process and never included in an error.
    ``boto3`` stays lazy so pure/local code has no AWS import dependency.
    """
    direct = os.environ.get(value_env, "").strip()
    secret_arn = os.environ.get(secret_arn_env, "").strip()
    if direct and secret_arn:
        raise RuntimeError(
            f"Set exactly one of {value_env} or {secret_arn_env}; refusing "
            "ambiguous secret configuration."
        )
    if direct:
        return direct
    if not secret_arn:
        raise RuntimeError(
            f"One of {value_env} or {secret_arn_env} is required deployment "
            "configuration and is not set."
        )

    cache_key = (secret_arn, json_key)
    cached = _secret_cache.get(cache_key)
    if cached is not None:
        return cached

    import boto3  # lazy: only AWS deployments need the SDK client
    import json

    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    raw = response.get("SecretString")
    if not isinstance(raw, str):
        raise RuntimeError(
            f"Secrets Manager secret configured by {secret_arn_env} has no text SecretString."
        )
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Secrets Manager secret configured by {secret_arn_env} must be a JSON object "
            f"containing {json_key!r}."
        ) from None
    value = document.get(json_key) if isinstance(document, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"Secrets Manager secret configured by {secret_arn_env} lacks a non-empty "
            f"{json_key!r} value."
        )
    _secret_cache[cache_key] = value
    return value


def require_dsn() -> str:
    """Resolve the DSN from local config or its dedicated AWS secret."""
    return require_secret_or_env(
        "STIGMERGY_DSN", "STIGMERGY_DSN_SECRET_ARN", json_key="dsn"
    )


def require_node_id() -> str:
    """
    STIGMERGY_NODE_ID, validated. Same rules as audit-side node ids:
    node_id is a PRIMARY KEY component in audit_chain — an unbounded or
    adversarial value here is a real footgun, not just an oddity.
    """
    node_id = require_env("STIGMERGY_NODE_ID")
    if len(node_id) > _MAX_NODE_ID_LEN:
        raise RuntimeError(
            f"STIGMERGY_NODE_ID too long ({len(node_id)} chars, "
            f"max {_MAX_NODE_ID_LEN})."
        )
    if not _NODE_ID_PATTERN.match(node_id):
        raise RuntimeError(
            "STIGMERGY_NODE_ID must contain only letters, digits, "
            "hyphens, and underscores."
        )
    return node_id


def env_int(name: str, default: int, minimum: int = 1) -> int:
    """
    Integer env parsing that rejects garbage loudly. A typo'd env var
    silently falling back to a default is a config change nobody made.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name}={raw!r} is not an integer.") from None
    if value < minimum:
        raise RuntimeError(f"{name}={value} is below the minimum {minimum}.")
    return value


def get_connection():
    """
    Lazy, reused psycopg connection. autocommit stays OFF — every unit
    of work goes through audit.chain.run_in_transaction, which owns
    commit/rollback/retry (40001) discipline.
    """
    global _conn
    import psycopg  # lazy: see module docstring

    if _conn is None or _conn.closed:
        _conn = psycopg.connect(require_dsn(), autocommit=False)
    return _conn


def require_deployment_authority(conn, node_id: str, *, capability: str | None = None):
    """Prove that this Lambda's DB credential owns its declared node.

    ``STIGMERGY_NODE_ID`` is deployment configuration, not authentication.
    This check binds it to CockroachDB's ``current_user`` on every valid
    invocation, including an otherwise no-op changefeed heartbeat.  A Lambda
    therefore cannot appear healthy while pointed at the wrong service-account
    secret.  When a global capability is supplied, prove that too before work
    begins; regional checks stay beside the operation because their region is
    input-dependent.

    The check deliberately uses the same transaction runner as state changes:
    it gets Cockroach serializable-retry behavior and never leaves an open
    transaction on a warm connection.
    """
    if capability is None:
        return run_in_transaction(
            conn, lambda cur: require_active_node(cur, node_id=node_id)
        )
    return run_in_transaction(
        conn,
        lambda cur: require_node_capability(
            cur, node_id=node_id, capability=capability,
        ),
    )


@dataclass(frozen=True)
class DrainSummary:
    chunks_run: int
    total: int
    drained: bool  # True: backlog exhausted. False: budget hit, work remains.


def bounded_drain(step, max_chunks: int, chunk_size: int | None = None) -> DrainSummary:
    """
    Run step() (each call = one bounded transaction returning the count
    it processed) until the backlog is empty or max_chunks is spent.

    If chunk_size is provided, a chunk that processed FEWER than
    chunk_size rows proves the backlog was empty at that instant (the
    LIMIT wasn't reached), so the drain stops one transaction earlier.
    Without it, only a zero-count chunk proves exhaustion.

    step() must return a non-negative int — anything else is a bug at
    the call site, rejected here rather than silently summed.
    """
    if isinstance(max_chunks, bool) or not isinstance(max_chunks, int):
        raise TypeError(f"max_chunks must be int, got {type(max_chunks).__name__}.")
    if max_chunks < 1:
        raise ValueError(f"max_chunks must be >= 1, got {max_chunks}.")
    if chunk_size is not None:
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise TypeError(f"chunk_size must be int, got {type(chunk_size).__name__}.")
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1, got {chunk_size}.")

    total = 0
    for i in range(max_chunks):
        n = step()
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError(f"drain step returned {type(n).__name__}, expected int.")
        if n < 0:
            raise ValueError(f"drain step returned {n} — counts cannot be negative.")
        total += n
        if n == 0 or (chunk_size is not None and n < chunk_size):
            return DrainSummary(chunks_run=i + 1, total=total, drained=True)
    return DrainSummary(chunks_run=max_chunks, total=total, drained=False)
