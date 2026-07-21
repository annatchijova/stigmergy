# Security audit — Round 2: authority, concurrency, and rollback

**Date:** 2026-07-21  
**Method:** adversarial A–D–I testing against disposable local CockroachDB
clusters (v26.2.2), not mock cursors.  
**Scope:** node authority, memory mutation, controller ownership, concurrent
reinforcement/migration, and authority-revocation rollback.

## Threat model

- An attacker can call reviewed application operations using a database
  principal they legitimately hold.
- They cannot modify reviewed runtime code, steal another DB credential, or act
  as a CockroachDB superuser.
- Each independently trusted node has a distinct database principal.

## Confirmed by induction

| ID | Vector | Observation |
|---|---|---|
| R2-01 | Node/region impersonation | A principal attempting to use another node id was rejected with `NodePrincipalMismatch`. |
| R2-02 | Post-revocation writes | Revoked nodes were rejected on store, reinforce, signal, resolve, and recall paths. |
| R2-03 | Cross-agent controller mutation | A node attempting `observe()` on another node's `agent_id` was rejected with `AgentOwnershipDenied`. |
| R2-04 | Concurrent reinforcement | Two simultaneous reinforcements produced exact states `0.6000000000` and `0.6800000000`; final confidence was `0.6800000000`; chain verified. |
| R2-05 | Concurrent migration | Two resolvers racing for one memory produced one migration, one named `ValueError` because it was already at the target, exactly one `MEMORY_MIGRATED`, and a valid chain. |
| R2-06 | Failure during revocation seal | Injecting an exception at `NODE_REVOKED` sealing left node and capability `ACTIVE`, administrator chain length `0`, and chain valid. No partial revocation committed. |
| R2-07 | Grant/revoke race | Concurrent `SIGNAL` grant and node revocation ended with the node `REVOKED`, no residual capability row, and the grant rejected as `NodeRevoked`. |
| R2-08 | In-flight write/revoke race | CockroachDB retried the writer after revocation; its renewed authority check raised `NodeRevoked`. No `MEMORY_STORED` event was committed after `NODE_REVOKED`. |

## What changed

- `agent_nodes` binds an audit node to CockroachDB `current_user`.
- Regional and global capabilities are closed vocabularies checked inside the
  same transaction as mutations.
- `REVOKED` blocks all reviewed state-changing paths.
- `agent_search_state.owner_node_id` prevents one authenticated node from
  steering another agent's controller state.
- The local secure demo uses separate principals/DSNs for seeder, agents, and
  resolver; it verifies individual chains and the Merkle ledger.
- `create_region()` is idempotent without poisoning a CockroachDB transaction.

## Reproduction

```bash
./tools/run_authority_integration.sh
./tools/run_secure_demo_local.sh
```

Both commands create localhost-only temporary clusters and remove their data
on exit. The second command exercises separate local DB users and reports
per-node chains plus a verified Merkle ledger.

## Boundary / remaining work

This is not Byzantine consensus, credential-theft resistance, or a CockroachDB
superuser defense. Cloud deployment still needs distinct service accounts,
least-privilege grants, credential rotation, AWS execution identities, and the
same integration suite against CockroachDB Cloud.
