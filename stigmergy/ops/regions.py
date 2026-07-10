"""
STIGMERGY — Region lifecycle. Phase 1: creation only.

Split and merge are the Phase 2 stretch goal, explicitly gated behind
CAS serialization on memory_regions.status (ARCHITECTURE.md, known
limitations). What Phase 1 DOES need is a way to bring a region into
existence without violating Invariant 3 — memory_regions is a table the
invariant's letter names, so a raw INSERT from a demo script or a setup
job would be exactly the "quiet patch" the architecture forbids.

Lineage discipline (Invariant 4), enforced at the boundary and pinned
by test: a ROOT region has generation 1 and no parent; a CHILD region
has a parent and generation > 1. The two arrive together or not at all —
a parentless generation-3 region is a lineage that cannot be
reconstructed, which defeats the reason lineage exists.

Shared-cursor contract, as everywhere: no commit here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from audit.chain import append_event

REGION_ID_MAX_LEN = 64
_REGION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class RegionExists(RuntimeError):
    """region_id already exists (PK 23505). Not corruption — creation is
    not idempotent by design; the caller decides whether existing is fine."""


@dataclass(frozen=True)
class CreatedRegion:
    region_id: str
    locality: str
    generation: int
    parent_region: str | None


def create_region(
    cur,
    *,
    node_id: str,
    region_id: str,
    locality: str,
    reason: str,
    parent_region: str | None = None,
    generation: int = 1,
) -> CreatedRegion:
    """
    Create one region (status ACTIVE) and seal REGION_CREATED in the
    caller's transaction.

    region_id is a vector index prefix and a foreign key target all over
    the schema — validated like node ids: max 64 chars, letters/digits/
    hyphen/underscore, must start alphanumeric.
    """
    if not isinstance(region_id, str) or not region_id.strip():
        raise ValueError("region_id is required and cannot be empty.")
    if len(region_id) > REGION_ID_MAX_LEN:
        raise ValueError(f"region_id too long ({len(region_id)} chars, "
                         f"max {REGION_ID_MAX_LEN}).")
    if not _REGION_ID_PATTERN.match(region_id):
        raise ValueError("region_id must be letters, digits, hyphens, or "
                         "underscores, starting alphanumeric.")
    if not isinstance(locality, str) or not locality.strip():
        raise ValueError("locality is required — a region without physical "
                         "placement conflates logical and physical (Invariant 1's "
                         "sibling sin).")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise TypeError(f"generation must be int, got {type(generation).__name__}.")
    if generation < 1:
        raise ValueError(f"generation must be >= 1, got {generation}.")
    if (parent_region is None) != (generation == 1):
        raise ValueError(
            "lineage violation (Invariant 4): a root region is generation 1 "
            "with no parent; a child region has a parent and generation > 1. "
            f"Got generation={generation}, parent_region={parent_region!r}."
        )
    if parent_region is not None and (
        not isinstance(parent_region, str) or not parent_region.strip()
    ):
        raise ValueError("parent_region, when given, must be a non-empty string.")

    try:
        cur.execute(
            """
            INSERT INTO memory_regions
                (region_id, locality, generation, parent_region, status)
            VALUES (%s, %s, %s, %s, 'ACTIVE')
            """,
            (region_id, locality, generation, parent_region),
        )
    except Exception as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate is None:
            diag = getattr(exc, "diag", None)
            sqlstate = getattr(diag, "sqlstate", None)
        if sqlstate == "23505":  # unique_violation: region already exists
            raise RegionExists(f"region {region_id!r} already exists.") from exc
        if sqlstate == "23503":  # foreign_key_violation: parent missing
            raise LookupError(
                f"parent_region {parent_region!r} does not exist."
            ) from exc
        raise

    append_event(
        cur,
        node_id=node_id,
        event_type="REGION_CREATED",
        reason=reason,
        payload={
            "region_id": region_id,
            "locality": locality,
            "generation": generation,
            "parent_region": parent_region,
        },
    )
    return CreatedRegion(region_id, locality, generation, parent_region)
