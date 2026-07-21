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
    "REGION_ADMIN",
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


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    db_principal: str


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
