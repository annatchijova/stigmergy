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

### Lambda deployment identity

- Added `require_deployment_authority()` in `lambdas.common`. A valid Lambda
  invocation now proves its configured node id belongs to the principal behind
  its actual CockroachDB DSN. A resolver performs this even for a heartbeat or
  another no-op batch; a sweeper also proves global `MAINTAIN` before it can
  report an empty successful run.
- Added `docs/AWS_COCKROACH_DEPLOYMENT_CONTRACT.md`: an explicit mapping from
  AWS role, secret, CockroachDB service account, node id, and minimum domain
  capability. It names the provisioning order and residual boundaries rather
  than implying that an environment variable is authentication.
- Extended the disposable real-cluster runner with
  `tests/test_lambda_authority_integration.py`. It creates three different
  CockroachDB users and confirms principal mismatch and missing `MAINTAIN`
  fail closed. This is execution evidence, not a mock claim.
- The real Lambda test exposed a privilege-design conflict: CockroachDB
  requires `UPDATE` privilege for `SELECT … FOR SHARE`. A proposed replacement
  with plain serializable reads was **falsified by induction**: after a writer
  read ACTIVE, a revocation could commit and the writer could still commit an
  unrelated operational update. The authority locks remain. The regression now
  proves their linear order: an already-authorized writer finishes, revocation
  then commits, and every later writer sees `NodeRevoked`. The required SQL
  `UPDATE` privilege over authority tables is documented as a residual
  credential/code-confinement boundary rather than misrepresented as
  least-privilege row protection.
- Moved `sweep_orphans()` naive-timestamp validation ahead of its authority
  query. Invalid time input is now rejected before opening a cursor operation,
  preserving the public boundary contract exercised by its pure test.

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
