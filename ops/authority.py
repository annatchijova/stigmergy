"""STIGMERGY — database-bound node authority.

An audit-chain ``node_id`` names the chain that recorded a write.  It does
not, by itself, prove that the database principal making the write was allowed
to speak for that node or for a logical memory region.  This module supplies
that missing boundary.

The database is the authority source.  A caller cannot establish identity by
passing a string: ``require_active_node`` binds the requested node to
CockroachDB's ``current_user`` inside the same transaction as the later state
change and audit event.  Region capabilities are then checked from the same
transactional view.

There is deliberately no permissive fallback for an unregistered node.  The
local demo must bootstrap explicit development identities; production uses one
least-privilege database principal per agent or Lambda role.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from audit.chain import append_event

_NODE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_NODE_ID_LEN = 64

# Keep this vocabulary closed.  A new state-changing operation has to declare
# the authority it consumes rather than accidentally inheriting broad write
# access from an unrelated operation.
VALID_REGION_CAPABILITIES = frozenset({
    "STORE",
    "REINFORCE",
    "SIGNAL",
    "RESOLVE",
    "OBSERVE",
})

VALID_NODE_CAPABILITIES = frozenset({
    "MAINTAIN",
    "REGION_ADMIN",
    "AUTHORITY_ADMIN",
    "RECALL_GLOBAL",
})


class AuthorityError(PermissionError):
    """Base class for a refused node-authority boundary."""


class UnknownNode(AuthorityError):
    """The requested audit node has no registered database identity."""


class NodeRevoked(AuthorityError):
    """The node was revoked and must not mutate shared state."""


class NodePrincipalMismatch(AuthorityError):
    """The database principal does not own the requested node id."""


class RegionCapabilityDenied(AuthorityError):
    """An active node lacks the named capability for the target region."""


class AdministratorDenied(AuthorityError):
    """The authenticated node is not an authority administrator."""


class NodeRegistrationConflict(RuntimeError):
    """A node id or DB principal is already registered to another node."""


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    db_principal: str


@dataclass(frozen=True)
class AuthorityChange:
    subject_node_id: str
    changed: bool


def validate_node_id(node_id: str) -> str:
    """Validate the public node-id grammar before issuing SQL."""
    if not isinstance(node_id, str) or not node_id.strip():
        raise ValueError("node_id is required and cannot be empty.")
    if len(node_id) > _MAX_NODE_ID_LEN:
        raise ValueError(
            f"node_id too long ({len(node_id)} chars, max {_MAX_NODE_ID_LEN})."
        )
    if not _NODE_ID_PATTERN.fullmatch(node_id):
        raise ValueError(
            "node_id must contain only letters, digits, hyphens, and underscores."
        )
    return node_id


def validate_region_capability(capability: str) -> str:
    """Reject an undeclared authority verb before it can reach SQL."""
    if not isinstance(capability, str) or capability not in VALID_REGION_CAPABILITIES:
        raise ValueError(
            "capability must be one of "
            f"{tuple(sorted(VALID_REGION_CAPABILITIES))}, got {capability!r}."
        )
    return capability


def validate_node_capability(capability: str) -> str:
    """Reject an undeclared non-regional authority verb before SQL."""
    if not isinstance(capability, str) or capability not in VALID_NODE_CAPABILITIES:
        raise ValueError(
            "capability must be one of "
            f"{tuple(sorted(VALID_NODE_CAPABILITIES))}, got {capability!r}."
        )
    return capability


def _require_text(value: str, name: str, *, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required and cannot be empty.")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} too long ({len(value)} chars, max {limit}).")
    return value


def require_active_node(cur, *, node_id: str) -> NodeIdentity:
    """Bind ``node_id`` to CockroachDB's authenticated current_user.

    ``FOR SHARE`` keeps the node's status stable until the surrounding
    transaction commits or rolls back: revocation cannot race a later write
    through a stale read.
    """
    node_id = validate_node_id(node_id)
    cur.execute(
        """
        SELECT db_principal, status
          FROM agent_nodes
         WHERE node_id = %s
           FOR SHARE
        """,
        (node_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise UnknownNode(
            f"node {node_id!r} is not registered; refusing unauthenticated write."
        )
    db_principal, status = row
    if status != "ACTIVE":
        raise NodeRevoked(
            f"node {node_id!r} has status {status!r}; refusing state mutation."
        )

    cur.execute("SELECT current_user")
    current_user = cur.fetchone()[0]
    if current_user != db_principal:
        raise NodePrincipalMismatch(
            f"database principal {current_user!r} is not authorized as node {node_id!r}."
        )
    return NodeIdentity(node_id=node_id, db_principal=db_principal)


def require_region_capability(
    cur,
    *,
    node_id: str,
    region_id: str,
    capability: str,
) -> NodeIdentity:
    """Require an active authenticated node with a live regional capability."""
    capability = validate_region_capability(capability)
    identity = require_active_node(cur, node_id=node_id)
    cur.execute(
        """
        SELECT status
          FROM node_region_capabilities
         WHERE node_id = %s
           AND region_id = %s
           AND capability = %s
           FOR SHARE
        """,
        (identity.node_id, region_id, capability),
    )
    row = cur.fetchone()
    if row is None or row[0] != "ACTIVE":
        state = "absent" if row is None else repr(row[0])
        raise RegionCapabilityDenied(
            f"node {identity.node_id!r} has no active {capability} capability "
            f"for region {region_id!r} (grant is {state})."
        )
    return identity


def require_node_capability(cur, *, node_id: str, capability: str) -> NodeIdentity:
    """Require an active authenticated node with a live global capability."""
    capability = validate_node_capability(capability)
    identity = require_active_node(cur, node_id=node_id)
    cur.execute(
        """
        SELECT status
          FROM node_capabilities
         WHERE node_id = %s
           AND capability = %s
           FOR SHARE
        """,
        (identity.node_id, capability),
    )
    row = cur.fetchone()
    if row is None or row[0] != "ACTIVE":
        state = "absent" if row is None else repr(row[0])
        raise RegionCapabilityDenied(
            f"node {identity.node_id!r} has no active {capability} capability "
            f"(grant is {state})."
        )
    return identity


def require_authority_administrator(cur, *, node_id: str) -> NodeIdentity:
    """Require the global capability and an out-of-band trusted principal."""
    identity = require_node_capability(
        cur, node_id=node_id, capability="AUTHORITY_ADMIN"
    )
    cur.execute(
        """
        SELECT 1
          FROM authority_administrators
         WHERE db_principal = %s
           FOR SHARE
        """,
        (identity.db_principal,),
    )
    if cur.fetchone() is None:
        raise AdministratorDenied(
            f"database principal {identity.db_principal!r} is not an authority administrator."
        )
    return identity


def register_node(
    cur,
    *,
    executor_node_id: str,
    node_id: str,
    db_principal: str,
    reason: str,
) -> AuthorityChange:
    """Register one node/principal binding and seal the authority change."""
    executor = require_authority_administrator(cur, node_id=executor_node_id)
    node_id = validate_node_id(node_id)
    db_principal = _require_text(db_principal, "db_principal", limit=128)
    reason = _require_text(reason, "reason")
    try:
        cur.execute(
            "INSERT INTO agent_nodes (node_id, db_principal) VALUES (%s, %s)",
            (node_id, db_principal),
        )
    except Exception as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate is None:
            sqlstate = getattr(getattr(exc, "diag", None), "sqlstate", None)
        if sqlstate == "23505":
            raise NodeRegistrationConflict(
                f"node {node_id!r} or principal {db_principal!r} is already registered."
            ) from exc
        raise
    append_event(
        cur,
        node_id=executor.node_id,
        event_type="NODE_REGISTERED",
        reason=reason,
        payload={"node_id": node_id, "db_principal": db_principal},
    )
    return AuthorityChange(subject_node_id=node_id, changed=True)


def _target_node_is_active(cur, node_id: str) -> None:
    cur.execute(
        "SELECT status FROM agent_nodes WHERE node_id = %s FOR SHARE", (node_id,)
    )
    row = cur.fetchone()
    if row is None:
        raise UnknownNode(f"node {node_id!r} is not registered.")
    if row[0] != "ACTIVE":
        raise NodeRevoked(f"node {node_id!r} has status {row[0]!r}.")


def grant_node_capability(
    cur,
    *,
    executor_node_id: str,
    node_id: str,
    capability: str,
    reason: str,
) -> AuthorityChange:
    """Grant a global capability; an already-active grant is a no-op."""
    executor = require_authority_administrator(cur, node_id=executor_node_id)
    node_id = validate_node_id(node_id)
    capability = validate_node_capability(capability)
    reason = _require_text(reason, "reason")
    _target_node_is_active(cur, node_id)
    cur.execute(
        "SELECT status FROM node_capabilities WHERE node_id = %s AND capability = %s FOR UPDATE",
        (node_id, capability),
    )
    row = cur.fetchone()
    if row is not None and row[0] == "ACTIVE":
        return AuthorityChange(subject_node_id=node_id, changed=False)
    if row is None:
        cur.execute(
            "INSERT INTO node_capabilities (node_id, capability) VALUES (%s, %s)",
            (node_id, capability),
        )
    else:
        cur.execute(
            """
            UPDATE node_capabilities
               SET status = 'ACTIVE', revoked_at = NULL, revoke_reason = NULL
             WHERE node_id = %s AND capability = %s
            """,
            (node_id, capability),
        )
    append_event(
        cur,
        node_id=executor.node_id,
        event_type="NODE_CAPABILITY_GRANTED",
        reason=reason,
        payload={"node_id": node_id, "capability": capability},
    )
    return AuthorityChange(subject_node_id=node_id, changed=True)


def grant_region_capability(
    cur,
    *,
    executor_node_id: str,
    node_id: str,
    region_id: str,
    capability: str,
    reason: str,
) -> AuthorityChange:
    """Grant one regional capability; an already-active grant is a no-op."""
    executor = require_authority_administrator(cur, node_id=executor_node_id)
    node_id = validate_node_id(node_id)
    capability = validate_region_capability(capability)
    reason = _require_text(reason, "reason")
    _target_node_is_active(cur, node_id)
    cur.execute(
        """
        SELECT status FROM node_region_capabilities
         WHERE node_id = %s AND region_id = %s AND capability = %s
           FOR UPDATE
        """,
        (node_id, region_id, capability),
    )
    row = cur.fetchone()
    if row is not None and row[0] == "ACTIVE":
        return AuthorityChange(subject_node_id=node_id, changed=False)
    if row is None:
        cur.execute(
            """
            INSERT INTO node_region_capabilities (node_id, region_id, capability)
            VALUES (%s, %s, %s)
            """,
            (node_id, region_id, capability),
        )
    else:
        cur.execute(
            """
            UPDATE node_region_capabilities
               SET status = 'ACTIVE', revoked_at = NULL, revoke_reason = NULL
             WHERE node_id = %s AND region_id = %s AND capability = %s
            """,
            (node_id, region_id, capability),
        )
    append_event(
        cur,
        node_id=executor.node_id,
        event_type="REGION_CAPABILITY_GRANTED",
        reason=reason,
        payload={
            "node_id": node_id,
            "region_id": region_id,
            "capability": capability,
        },
    )
    return AuthorityChange(subject_node_id=node_id, changed=True)


def revoke_node(
    cur,
    *,
    executor_node_id: str,
    node_id: str,
    reason: str,
) -> AuthorityChange:
    """Revoke a node and every active capability in one audited transaction."""
    executor = require_authority_administrator(cur, node_id=executor_node_id)
    node_id = validate_node_id(node_id)
    reason = _require_text(reason, "reason")
    _target_node_is_active(cur, node_id)
    cur.execute(
        """
        UPDATE agent_nodes
           SET status = 'REVOKED', revoked_at = now(), revoke_reason = %s
         WHERE node_id = %s AND status = 'ACTIVE'
        """,
        (reason, node_id),
    )
    cur.execute(
        """
        UPDATE node_capabilities
           SET status = 'REVOKED', revoked_at = now(), revoke_reason = %s
         WHERE node_id = %s AND status = 'ACTIVE'
        """,
        (reason, node_id),
    )
    node_caps_revoked = cur.rowcount
    cur.execute(
        """
        UPDATE node_region_capabilities
           SET status = 'REVOKED', revoked_at = now(), revoke_reason = %s
         WHERE node_id = %s AND status = 'ACTIVE'
        """,
        (reason, node_id),
    )
    region_caps_revoked = cur.rowcount
    append_event(
        cur,
        node_id=executor.node_id,
        event_type="NODE_REVOKED",
        reason=reason,
        payload={
            "node_id": node_id,
            "node_capabilities_revoked": node_caps_revoked,
            "region_capabilities_revoked": region_caps_revoked,
        },
    )
    return AuthorityChange(subject_node_id=node_id, changed=True)
