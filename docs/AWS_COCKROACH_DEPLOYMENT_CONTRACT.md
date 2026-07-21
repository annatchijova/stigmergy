# AWS Lambda and CockroachDB deployment contract

This document turns STIGMERGY's local authority model into a deployment
contract for CockroachDB Cloud and AWS Lambda. It is deliberately useful
before an AWS account or CockroachDB Cloud cluster exists: the names below are
the identities that must be provisioned, not an aspirational diagram.

## Invariant

An AWS execution role is **not** a STIGMERGY node identity. The node identity
is the three-way binding below:

```text
one Lambda function / agent process
    -> one CockroachDB least-privilege service account
    -> one registered ACTIVE agent_nodes row
    -> one STIGMERGY_NODE_ID environment value
```

The database stores the binding from node id to `db_principal`. At every valid
Lambda invocation, `require_deployment_authority()` opens a transaction and
checks CockroachDB `current_user`. This catches an incorrectly attached secret
even for a changefeed heartbeat or an otherwise empty sweep. It is not enough
for deployment configuration to *say* the node id.

## Required identity map

Use distinct database service accounts and distinct AWS Secrets Manager
secrets. The names are examples; preserving the one-to-one relationship is
the requirement.

| workload | AWS execution role | CockroachDB principal | `STIGMERGY_NODE_ID` | minimum STIGMERGY authority |
| --- | --- | --- | --- | --- |
| changefeed resolver Lambda | `stigmergy-resolver-lambda` | `stigmergy_resolver` | `resolver-prod` | `RESOLVE` only for regions it may resolve |
| EventBridge sweeper Lambda | `stigmergy-sweeper-lambda` | `stigmergy_sweeper` | `sweeper-prod` | global `MAINTAIN` |
| seeding / migration job | `stigmergy-seeder-job` | `stigmergy_seeder` | `seeder-prod` | `REGION_ADMIN`, plus regional `STORE` |
| each autonomous agent | unique task role | unique service account | unique node id | only its `OBSERVE`, `REINFORCE`, `SIGNAL`, regional `STORE`; add `RECALL_GLOBAL` only when required |
| authority administration | tightly controlled break-glass/job role | `stigmergy_authority_admin` | `authority-controller` | `AUTHORITY_ADMIN` and an `authority_administrators` row |

Never reuse a `STIGMERGY_DSN` secret across independently trusted nodes. A
shared DSN collapses the principal boundary and makes node identities labels
rather than authority. CockroachDB requires `UPDATE` table privilege for the
`SELECT … FOR SHARE` authority locks, so runtime principals require `SELECT`
and `UPDATE` on the authority tables. This is an explicit residual boundary:
those credentials must be reachable only by reviewed runtime code and never
be exposed as a general SQL client capability. CockroachDB does not provide a
row-limited *lock-only* privilege in this design. A real induced test showed
that replacing the lock with a plain serializable read allows a stale authority
read and an unrelated write to commit, so pretending the broader privilege is
unnecessary would be less safe, not more.

## Provisioning order

1. Create the CockroachDB cluster and a separate service account / SQL user
   for each row above. Grant each only the SQL privileges required by the
   application's tables; do not give Lambda principals cluster-admin access.
2. Apply `schema.sql` with the controlled migration principal.
3. Perform the intentionally out-of-band bootstrap in
   [AUTHORITY_MODEL.md](../AUTHORITY_MODEL.md): register the authority
   administrator and its matching node. Record that bootstrap separately.
4. Using that authenticated authority node, register every production node and
   make audited, least-privilege capability grants. Do not insert future agent
   rows directly as part of ordinary deployment.
5. Store each account's connection string in a separate AWS Secrets Manager
   secret. Permit exactly its matching Lambda/task role to read that one
   secret. Set `STIGMERGY_DSN` from it and set the matching
   `STIGMERGY_NODE_ID` as a non-secret environment variable.
6. Deploy the function, then invoke a harmless path before connecting live
   traffic: a resolver heartbeat and a sweeper tick. Both must prove the
   principal↔node binding. The sweeper must also prove `MAINTAIN`.
7. Only then create the CockroachDB changefeed and EventBridge schedule.

## Capability placement

The resolver cannot be granted a blanket administrative role merely because it
handles events. `resolve_recruitment()` verifies its `RESOLVE` grant for the
specific target region within the same transaction as the migration and audit
event. The sweeper has one global operation by design, so it declares and
checks the closed `MAINTAIN` capability before any drain starts.

This keeps AWS identity, database authentication, domain authority, state
transition, and audit event distinct. AWS controls *which process may read a
secret*; CockroachDB proves *which principal connected*; STIGMERGY proves
*which node and operation that principal may represent*.

## CockroachDB tooling boundary

When the CockroachDB Managed MCP Server is enabled, retain its safe read-only
mode for inspection and audit work. Do not give an MCP-connected model an
application mutation or authority-administration principal. Use the ccloud
CLI or a reviewed deployment job for cluster provisioning, service-account
rotation, and backup operations; those actions are outside a Lambda's runtime
authority.

## Failure handling

- `NodePrincipalMismatch` means the function has the wrong database
  credential for its declared node. Disable its trigger, inspect secret/role
  attachment, and do not retry blindly.
- `NodeRevoked` means the credential's node was deliberately revoked. It is a
  security state, not a transient Lambda error; provision a new node/secret
  through the audited authority path.
- `RegionCapabilityDenied` means the deployment has less authority than the
  code path requests. Review whether a grant is justified; do not broaden the
  node to an administrator by default.

## What this does not guarantee

This contract does not defend against a CockroachDB superuser, a stolen
service-account secret, compromised AWS credentials, or unreviewed code
running under a legitimate role. The authority-lock privilege requirement
makes the last two especially important. Those are explicit residual trust
boundaries, not claims the application should hide. Rotate compromised
credentials, revoke the associated STIGMERGY node, and inspect its sealed
audit chain.
