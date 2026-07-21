# STIGMERGY authority model

The audit chain answers **what node recorded a transition**. It does not, on
its own, answer whether that node was allowed to act. This module supplies the
second answer.

## The rule

Every mutable operation runs in one CockroachDB transaction:

```text
authenticated database principal
        -> registered ACTIVE node
        -> exact global or regional capability
        -> state transition + audit event
        -> commit together, or roll back together
```

`node_id` is not a caller-controlled label. `ops.authority` checks that
CockroachDB `current_user` equals the registered `db_principal` before a
write. A `REVOKED` node cannot store, reinforce, signal, resolve, create a
region, or run maintenance.

The roaming/dwelling controller follows the same rule: its first
`observe()` records `owner_node_id`, and later observations from any other
authenticated node are rejected. An active node may not steer another
agent's search state merely by naming its `agent_id`.

## Capabilities

Regional: `STORE`, `REINFORCE`, `SIGNAL`, `RESOLVE`, `OBSERVE`. A recall in
one region needs `OBSERVE` there.

Global: `MAINTAIN`, `REGION_ADMIN`, `RECALL_GLOBAL`, and `AUTHORITY_ADMIN`.
Roaming recall across all regions needs `RECALL_GLOBAL`; the last also
requires its database principal to appear in `authority_administrators`.
All grants and revocations are sealed into the administrator node's chain.

## Bootstrap

Bootstrap is deliberately out of band: use a CockroachDB administrator and a
distinct least-privilege DB principal for every independently trusted agent or
Lambda. Never share one application principal among nodes that must not be
able to impersonate each other.

After applying `schema.sql`, create the first authority node (replace names):

```sql
INSERT INTO authority_administrators (db_principal) VALUES ('ops-admin');
INSERT INTO agent_nodes (node_id, db_principal)
VALUES ('authority-controller', 'ops-admin');
INSERT INTO node_capabilities (node_id, capability) VALUES
  ('authority-controller', 'AUTHORITY_ADMIN'),
  ('authority-controller', 'REGION_ADMIN');
```

Then use audited `ops.authority.register_node`, `grant_node_capability`,
`grant_region_capability`, and `revoke_node` calls in normal transactions.
Each Lambda needs its own CockroachDB service account and matching registered
`STIGMERGY_NODE_ID`.

## Boundary

This prevents an application-level agent with one legitimate principal from
claiming another node or region. A CockroachDB superuser, stolen DB credential,
or code modified outside the reviewed runtime remains outside this guarantee.
