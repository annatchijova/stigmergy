"""
STIGMERGY — Lambda 1 of 2: changefeed-driven recruitment resolver.

CockroachDB pushes recruitment_signals changes to this function through
a webhook-sink changefeed (event-driven — deliberately NOT the same
pattern as the cron sweeper, per ARCHITECTURE.md):

    CREATE CHANGEFEED FOR TABLE recruitment_signals
      INTO 'webhook-https://<function-url>?insecure_tls_skip_verify=false'
      WITH updated, resolved = '30s';

Reaction rule: a NEW PENDING signal is a region raising its hand —
"I recruit this memory". The handler attempts ONE consensus resolution
per distinct (memory_id, origin_region) pair in the batch, with the
signal's origin as the target (a signal recruits TO its origin, the
REC-002 semantics). Rows whose after-state is not PENDING are ignored:
ACCEPTED/REJECTED/EXPIRED updates are resolutions and sweeps we
ourselves committed — reacting to them would be a feedback loop.

Delivery semantics, and why they are safe:
  - The webhook sink is AT-LEAST-ONCE. Redelivered inserts re-attempt a
    resolution that already happened; resolve_recruitment then finds the
    signals consumed (no longer PENDING), computes an empty live set,
    and returns not-reached — a no-op. Idempotence comes from the state
    machine, not from deduplication bookkeeping.
  - EXPECTED outcomes are results, not errors: consensus not reached,
    CooldownActive, RegionUnavailable, LookupError each classify into
    the summary and the batch still ACKs (200). The system preserved
    uncertainty; there is nothing to retry.
  - An UNEXPECTED exception marks the batch 500 so the sink redelivers —
    safe precisely because attempts are idempotent (above). Each attempt
    runs in ITS OWN transaction (run_in_transaction, 40001 retries
    inside); one failure never rolls back its neighbors.
  - MALFORMED entries ACK with a named report, never 500. The sink
    retries a batch until it gets 2xx: a poison message answered with
    500 stalls the entire changefeed behind it. Explicit degradation
    (the malformation is named in the response body and in logs) over a
    silently wedged feed. If malformations appear, that is schema/version
    skew and a human must look — the report is the alarm.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass

from audit.chain import run_in_transaction
from ops.recruitment import (
    CooldownActive, RegionUnavailable, resolve_recruitment,
)
from .common import get_connection, require_deployment_authority, require_node_id


_CHANGEFEED_TOKEN_HEADER = "x-stigmergy-changefeed-token"


def _configured_changefeed_token() -> str:
    """Read the resolver's ingress secret without accepting an empty value."""
    token = os.environ.get("STIGMERGY_CHANGEFEED_TOKEN", "")
    if not token:
        raise RuntimeError(
            "STIGMERGY_CHANGEFEED_TOKEN is required for the public changefeed "
            "ingress; refusing an unauthenticated resolver deployment."
        )
    return token


def changefeed_request_is_authenticated(event) -> bool:
    """Verify CockroachDB's configured extra header before touching the DB.

    Function URL/API Gateway header casing is not stable, so normalize keys.
    ``compare_digest`` avoids a token-prefix timing oracle at this public
    ingress boundary. A direct invocation without HTTP headers is deliberately
    not a production-compatible resolver call.
    """
    token = _configured_changefeed_token()
    if not isinstance(event, dict):
        return False
    headers = event.get("headers")
    if not isinstance(headers, dict):
        return False
    presented = next(
        (
            value for key, value in headers.items()
            if isinstance(key, str)
            and key.lower() == _CHANGEFEED_TOKEN_HEADER
            and isinstance(value, str)
        ),
        None,
    )
    return presented is not None and hmac.compare_digest(presented, token)


@dataclass(frozen=True)
class ResolutionAttempt:
    memory_id: str
    target_region: str


@dataclass(frozen=True)
class ParsedBatch:
    attempts: list[ResolutionAttempt]   # deduplicated, first-seen order
    malformed: list[str]                # named reasons, one per bad entry
    ignored: int                        # tombstones, non-PENDING, heartbeats


def parse_changefeed_envelope(envelope) -> ParsedBatch:
    """
    Pure parser for the webhook sink's JSON envelope:

        {"payload": [{"after": {...}, "key": [...], "updated": "..."},
                     ...],
         "length": N}

    Resolved-timestamp heartbeats ({"resolved": "..."}) and tombstones
    (after = null) are IGNORED, not malformed — they are the protocol
    working. Malformed means an entry we cannot interpret: those are
    counted, named, and reported, never silently dropped.
    """
    if not isinstance(envelope, dict):
        return ParsedBatch([], [f"envelope is {type(envelope).__name__}, expected object"], 0)
    if "resolved" in envelope and "payload" not in envelope:
        return ParsedBatch([], [], 1)  # heartbeat envelope

    payload = envelope.get("payload")
    if not isinstance(payload, list):
        return ParsedBatch([], ["envelope.payload is missing or not a list"], 0)

    attempts: list[ResolutionAttempt] = []
    seen: set[tuple[str, str]] = set()
    malformed: list[str] = []
    ignored = 0

    for i, entry in enumerate(payload):
        if not isinstance(entry, dict):
            malformed.append(f"payload[{i}]: not an object")
            continue
        if "resolved" in entry and "after" not in entry:
            ignored += 1  # heartbeat row
            continue
        after = entry.get("after")
        if after is None:
            ignored += 1  # tombstone/delete — nothing to react to
            continue
        if not isinstance(after, dict):
            malformed.append(f"payload[{i}]: 'after' is {type(after).__name__}, expected object")
            continue
        status = after.get("status")
        if status != "PENDING":
            ignored += 1  # our own resolutions/sweeps echoing back
            continue
        memory_id = after.get("memory_id")
        origin = after.get("origin_region")
        if not isinstance(memory_id, str) or not memory_id.strip():
            malformed.append(f"payload[{i}]: PENDING signal without a usable memory_id")
            continue
        if not isinstance(origin, str) or not origin.strip():
            malformed.append(f"payload[{i}]: PENDING signal without a usable origin_region")
            continue
        pair = (memory_id, origin)
        if pair in seen:
            ignored += 1  # duplicate within the batch: one attempt suffices
            continue
        seen.add(pair)
        attempts.append(ResolutionAttempt(memory_id=memory_id, target_region=origin))

    return ParsedBatch(attempts=attempts, malformed=malformed, ignored=ignored)


def _attempt(conn, node_id: str, attempt: ResolutionAttempt) -> str:
    """
    One resolution attempt, one transaction, classified into a summary
    key. Expected outcomes return; unexpected exceptions propagate to
    the handler, which answers 500 (redeliver).
    """
    try:
        outcome = run_in_transaction(
            conn,
            lambda cur: resolve_recruitment(
                cur,
                node_id=node_id,
                memory_id=attempt.memory_id,
                target_region=attempt.target_region,
            ),
        )
        return "migrated" if outcome.reached else "not_reached"
    except CooldownActive:
        return "cooldown"
    except RegionUnavailable:
        return "region_unavailable"
    except LookupError:
        return "lookup_error"
    except ValueError:
        # Same-region target (REC-005): the memory already lives where
        # the signal points — a legitimate no-op, not an incident.
        return "already_there"


def handler(event, context=None):
    # This endpoint must be reachable by CockroachDB's HTTPS webhook, not by
    # arbitrary callers. Reject before parsing or connecting so an unauthenticated
    # request cannot consume database work or trigger a resolution attempt.
    if not changefeed_request_is_authenticated(event):
        return {"statusCode": 401, "body": json.dumps({"error": "unauthorized changefeed ingress"})}

    node_id = require_node_id()

    # Function-URL / API-Gateway invocations wrap the sink's JSON in a
    # string body; direct invocations hand the envelope itself.
    body = event.get("body") if isinstance(event, dict) else None
    if isinstance(body, str):
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            return {"statusCode": 400,
                    "body": json.dumps({"error": f"body is not JSON: {exc}"})}
    else:
        envelope = event

    parsed = parse_changefeed_envelope(envelope)

    summary = {"migrated": 0, "not_reached": 0, "cooldown": 0,
               "region_unavailable": 0, "lookup_error": 0,
               "already_there": 0}
    failures: list[str] = []
    # Validate even a heartbeat/no-op batch.  A valid invocation must never
    # look healthy if its STIGMERGY_NODE_ID and database service account drift.
    conn = get_connection()
    require_deployment_authority(conn, node_id)

    for attempt in parsed.attempts:
        try:
            summary[_attempt(conn, node_id, attempt)] += 1
        except Exception as exc:  # unexpected: name it, redeliver batch
            failures.append(
                f"{attempt.memory_id}->{attempt.target_region}: "
                f"{type(exc).__name__}: {exc}"
            )

    body_out = {
        "attempts": len(parsed.attempts),
        "summary": summary,
        "ignored": parsed.ignored,
        "malformed": parsed.malformed,   # ACKed but named — the alarm
        "failures": failures,
    }
    status = 500 if failures else 200
    return {"statusCode": status, "body": json.dumps(body_out)}
