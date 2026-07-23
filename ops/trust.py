"""
STIGMERGY — Actor quarantine and deterministic taint propagation.

Ported from MNEME (mneme/trust.py). The scenario this module exists for:
a data source (a compromised agent node, a poisoned ingestion pipeline)
was feeding bad content into the shared field. The question is never
"delete its memories" — Invariant 8 forbids deletion, and deletion would
also destroy the evidence. The question is:

    "Show me EVERYTHING this node touched, flag all of it so recall
     stops serving it, seal the flagging itself so nobody can later
     dispute what was flagged — and make the whole operation
     reproducible from the audit trail alone."

Definition of "touched" (deliberately broad, documented, testable): a
memory is tainted by node X if any event in its custody chain names X as
actor_id — EXCEPT CONTRADICTED_BY. Not just STORED — a poisoned node that
REINFORCED a legitimate memory inflated its confidence, and that
inflation is part of the incident. Analysts REHABILITATE false positives
via an audited custody event; over-flagging is recoverable, under-
flagging is not.

The CONTRADICTED_BY carve-out is an architecture decision, not a
softening: when X stores a memory that contradicts memory V, the
contradiction event lands on V's chain with X as its author — the one
event type through which a node writes its identity onto an ARBITRARY
victim's chain. Counting it as "touched" hands an attacker a lever:
contradict every truth you want suppressed, and the day you're
quarantined, the sweep silences your own victims for you. Taint tracks
INFLUENCE (events that created a memory or raised its standing); being
attacked by X is not influence by X.

Orthogonality (design call, see the port's plan document): taint here is
independent of agent_nodes.status (ACTIVE/REVOKED). Revocation is an
authority question — can this node still authenticate and write, decided
once, going forward. Taint is an evidence question — what did this node
write that we no longer trust, decided per-memory, about the past. A
node can be revoked with clean history; an active node can be mid-
investigation. There is no stored "quarantined" status on agent_nodes;
"has node X ever been swept" is a derived read against taint_sweeps
(UNIQUE(quarantined_actor) makes a second sweep for the same node a
constraint violation, not a race outcome — the same idempotence MNEME
enforced in application code, enforced here at the schema level too).

Authority gating: quarantine_actor is system-wide and cross-region, so
it requires ops.authority.require_authority_administrator — the SAME
tier revoke_node and register_node already use, not merely
require_node_capability("AUTHORITY_ADMIN"), because that is the
established pattern for STIGMERGY's most consequential operations.
quarantine_memory / rehabilitate_memory act on one memory, gated with
require_node_capability(..., "REGION_ADMIN") — matching
ops.regions.create_region's existing precedent for that capability,
which (despite the name) is a global node capability in this schema, not
a per-region grant (see ops/authority.py's VALID_NODE_CAPABILITIES).

Determinism: the flagged set is one SQL query with a total ORDER BY; the
sweep seals sha256(canonical_json({"memory_ids": sorted_ids})), so two
replays of the same database state produce byte-identical sweep evidence.

As everywhere in this codebase: every function here takes a live cursor
and NEVER commits — the caller's transaction carries both the state
change and its custody event, or neither happens.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from audit.canonical import canonical_json
from audit import custody as memory_custody

from .authority import require_authority_administrator, require_node_capability


@dataclass(frozen=True)
class TaintSweep:
    sweep_id: str
    quarantined_actor: str
    initiated_by: str
    reason: str
    flagged_memory_ids: tuple[str, ...]
    flagged_ids_sha256: str
    advisory_resonant_neighbours: tuple[str, ...]


def _seal_ids(memory_ids: list[str]) -> str:
    return hashlib.sha256(
        canonical_json({"memory_ids": memory_ids}).encode("utf-8")
    ).hexdigest()


def quarantine_actor(
    cur,
    *,
    executor_node_id: str,
    actor_id: str,
    reason: str,
) -> TaintSweep:
    """
    Quarantine node `actor_id` and taint-flag every memory whose custody
    chain names it (except CONTRADICTED_BY). One logical operation, one
    transaction (the caller's):

      1. AUTHORITY_ADMIN + authority-administrator check on the executor.
      2. actor_id must be a registered agent_nodes row (ACTIVE or REVOKED
         — quarantine targets identity, not current authentication state).
      3. Idempotence: refuse if taint_sweeps already has a row for this
         actor (schema UNIQUE(quarantined_actor) is the last line of
         defense; this is the first, with a clearer error).
      4. Deterministic flagged set from custody_chain, ORDER BY memory_id.
      5. Per memory: TAINT_FLAGGED custody event + custody_status update
         (only if currently CLEAN — QUARANTINED memories keep their
         stronger status, but the event is still written: the chain
         records that the sweep saw them).
      6. Seal the sorted flagged-id list into one taint_sweeps row.
      7. Advisory one-hop RESONANT neighbours of the flagged set that are
         NOT themselves flagged — reported, never auto-flagged (see
         module docstring's KNOWN_LIMITATIONS pointer).
    """
    executor = require_authority_administrator(cur, node_id=executor_node_id)

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be non-empty — an unreasoned quarantine cannot exist.")

    cur.execute("SELECT node_id FROM agent_nodes WHERE node_id = %s FOR SHARE", (actor_id,))
    if cur.fetchone() is None:
        raise ValueError(f"Unknown node {actor_id!r} — quarantine targets a registered agent_nodes identity.")

    cur.execute("SELECT 1 FROM taint_sweeps WHERE quarantined_actor = %s", (actor_id,))
    if cur.fetchone() is not None:
        raise ValueError(
            f"Node {actor_id!r} already has a taint sweep on record. A second "
            "sweep for the same incident would split the evidence across two "
            "sweep ids; there is no per-actor un-sweep in Phase 1."
        )

    cur.execute(
        "SELECT DISTINCT memory_id FROM custody_chain WHERE actor_id = %s "
        "AND event_type != 'CONTRADICTED_BY' ORDER BY memory_id ASC",
        (actor_id,),
    )
    flagged = [str(r[0]) for r in cur.fetchall()]

    sweep_id = str(uuid.uuid4())
    payload_common: dict[str, Any] = {"sweep_id": sweep_id, "quarantined_actor": actor_id}

    for mid in flagged:
        memory_custody.append_event(
            cur,
            memory_id=mid,
            event_type="TAINT_FLAGGED",
            actor_id=executor.node_id,
            reason=reason,
            payload=dict(payload_common),
        )
        cur.execute(
            "UPDATE memories SET custody_status = 'TAINT_FLAGGED' "
            "WHERE id = %s AND custody_status = 'CLEAN'",
            (mid,),
        )

    seal = _seal_ids(sorted(flagged))
    cur.execute(
        """
        INSERT INTO taint_sweeps
            (sweep_id, quarantined_actor, initiated_by, reason,
             flagged_count, flagged_ids_sha256)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (sweep_id, actor_id, executor.node_id, reason, len(flagged), seal),
    )

    # Advisory one-hop RESONANT neighbourhood. cell_links stores one row
    # per unordered pair (memory_id_a < memory_id_b, canonicalized) —
    # unlike MNEME's directional from_id/to_id, so both columns are
    # checked and the OTHER side of each matching pair is reported.
    neighbours: list[str] = []
    if flagged:
        cur.execute(
            """
            SELECT DISTINCT other FROM (
                SELECT memory_id_b::STRING AS other
                  FROM cell_links
                 WHERE link_type = 'RESONANT' AND memory_id_a = ANY(%s::UUID[])
                UNION
                SELECT memory_id_a::STRING AS other
                  FROM cell_links
                 WHERE link_type = 'RESONANT' AND memory_id_b = ANY(%s::UUID[])
            ) t
            WHERE other != ALL(%s::UUID[])
            ORDER BY other ASC
            """,
            (flagged, flagged, flagged),
        )
        neighbours = [r[0] for r in cur.fetchall()]

    return TaintSweep(
        sweep_id=sweep_id,
        quarantined_actor=actor_id,
        initiated_by=executor.node_id,
        reason=reason,
        flagged_memory_ids=tuple(flagged),
        flagged_ids_sha256=seal,
        advisory_resonant_neighbours=tuple(neighbours),
    )


def quarantine_memory(
    cur,
    *,
    executor_node_id: str,
    memory_id: str,
    reason: str,
) -> None:
    """
    Direct quarantine of ONE memory: the analyst has evidence against
    this memory itself (not merely against a node in its chain).
    QUARANTINED custody event + status update, one transaction.

    Allowed from any status except QUARANTINED itself — re-quarantining
    is refused, the second incident's evidence belongs in the chain's
    next event, not in a duplicate status write. There is deliberately
    NO reversal here: rehabilitate_memory reverses TAINT_FLAGGED only:
    undoing a direct quarantine is a stronger claim with no designed
    review path yet (see KNOWN_LIMITATIONS.md).
    """
    executor = require_node_capability(cur, node_id=executor_node_id, capability="REGION_ADMIN")

    cur.execute("SELECT custody_status FROM memories WHERE id = %s FOR UPDATE", (memory_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Unknown memory {memory_id!r}.")
    if row[0] == "QUARANTINED":
        raise ValueError(
            f"{memory_id} is already QUARANTINED — a duplicate quarantine "
            "would add a status write with no new evidence."
        )

    memory_custody.append_event(
        cur, memory_id=memory_id, event_type="QUARANTINED",
        actor_id=executor.node_id, reason=reason, payload={},
    )
    cur.execute(
        "UPDATE memories SET custody_status = 'QUARANTINED' WHERE id = %s",
        (memory_id,),
    )


def rehabilitate_memory(
    cur,
    *,
    executor_node_id: str,
    memory_id: str,
    reason: str,
) -> None:
    """
    Audited reversal of TAINT_FLAGGED for one memory (analyst reviewed a
    false positive). QUARANTINED memories are NOT rehabilitated here —
    direct quarantine requires its own review path; conflating the two
    reversals would let a bulk false-positive cleanup silently
    un-quarantine directly-incriminated memories.
    """
    executor = require_node_capability(cur, node_id=executor_node_id, capability="REGION_ADMIN")

    cur.execute("SELECT custody_status FROM memories WHERE id = %s FOR UPDATE", (memory_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Unknown memory {memory_id!r}.")
    if row[0] != "TAINT_FLAGGED":
        raise ValueError(
            f"{memory_id} has custody_status {row[0]!r}; rehabilitate_memory "
            "reverses TAINT_FLAGGED only."
        )

    memory_custody.append_event(
        cur, memory_id=memory_id, event_type="REHABILITATED",
        actor_id=executor.node_id, reason=reason,
        payload={"from_status": "TAINT_FLAGGED"},
    )
    cur.execute(
        "UPDATE memories SET custody_status = 'CLEAN' WHERE id = %s",
        (memory_id,),
    )


def verify_sweep(cur, sweep_id: str) -> tuple[bool, list[str]]:
    """
    Re-derive a sweep's flagged set from custody evidence and check it
    against the sealed hash. The claim "we flagged exactly these"
    becomes checkable: the set of memories carrying a TAINT_FLAGGED
    event whose payload names this sweep_id must hash to
    flagged_ids_sha256.
    """
    import json as _json

    cur.execute(
        "SELECT flagged_count, flagged_ids_sha256 FROM taint_sweeps WHERE sweep_id = %s",
        (sweep_id,),
    )
    row = cur.fetchone()
    if row is None:
        return False, [f"Unknown sweep {sweep_id!r}."]
    count, seal = int(row[0]), str(row[1])

    cur.execute(
        "SELECT memory_id, payload_json FROM custody_chain "
        "WHERE event_type = 'TAINT_FLAGGED' ORDER BY memory_id ASC",
    )
    ids: list[str] = []
    for mid, pj in cur.fetchall():
        if isinstance(pj, (bytes, bytearray)):
            pj = pj.decode("utf-8", errors="replace")
        try:
            payload = _json.loads(pj) if isinstance(pj, str) else pj
        except Exception:
            return False, [f"{mid}: TAINT_FLAGGED payload is not valid JSON."]
        if payload.get("sweep_id") == sweep_id:
            ids.append(str(mid))

    errors: list[str] = []
    if len(ids) != count:
        errors.append(
            f"Sweep {sweep_id}: {len(ids)} TAINT_FLAGGED events found, row claims {count}."
        )
    if _seal_ids(sorted(ids)) != seal:
        errors.append(f"Sweep {sweep_id}: flagged set does not hash to the seal.")
    return (not errors), errors
