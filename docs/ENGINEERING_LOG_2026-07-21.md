# Engineering log — 2026-07-21

## Objective

Turn STIGMERGY's documented distributed-memory design into a system with an
enforced application-level authority boundary before CockroachDB Cloud and AWS
deployment work begins.

## Implemented

### Database-bound node authority

- Added `agent_nodes`, global `node_capabilities`, and regional
  `node_region_capabilities` to `schema.sql`.
- Bound each `node_id` to CockroachDB `current_user`; a string passed by a
  caller is not identity.
- Added closed authority vocabularies and transactional checks in
  `ops.authority`.
- Added audited administrator operations: register node, grant global or
  regional capability, and revoke node plus all live grants.

### Mutation coverage

| Operation | Required authority |
|---|---|
| Store | `STORE` for target region |
| Reinforce | `REINFORCE` for memory region |
| Regional recall | `OBSERVE` for region |
| Roaming recall | `RECALL_GLOBAL` |
| Recruitment signal | `SIGNAL` for origin region |
| Resolve/migrate | `RESOLVE` for target region |
| Expiry/orphan sweep | `MAINTAIN` |
| Create region | `REGION_ADMIN` |
| Change authority | `AUTHORITY_ADMIN` plus trusted DB principal |

`agent_search_state` now records `owner_node_id`; another authenticated node
cannot update an existing agent's controller state.

### Demo and reproducibility

- Added `tools/run_authority_integration.sh`: temporary local CockroachDB,
  authority/impersonation/revocation regression, automatic cleanup.
- Added `tools/run_secure_demo_local.sh`: separate local principals for
  seeder, each agent, and resolver; audited bootstrap; per-node chain and
  Merkle-ledger verification.
- Corrected `create_region()` to use `ON CONFLICT DO NOTHING`, preventing an
  expected duplicate seed from aborting a CockroachDB transaction.

## Evidence

The executed adversarial experiments are in
`docs/SECURITY_AUDIT_ROUND_2.md`. They include authority impersonation,
revocation, ownership, concurrent reinforce/migrate, grant/revoke, in-flight
write/revoke, and rollback under injected seal failure.

## Deliberate boundaries

- No claim of Byzantine consensus, protection from a CockroachDB superuser, or
  protection after credential theft.
- CockroachDB Cloud service accounts, Cloud MCP, ccloud CLI, vector-index
  production configuration, and AWS Lambdas remain the next deployment phase.
- The local runners are reproducible development evidence, not substitutes for
  Cloud deployment validation.
