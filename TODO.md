# STIGMERGY — next work

**Status at handoff:** local engineering, authority hardening, secure demo,
AWS SAM scaffold, and red-team evidence are committed on `main`. Nothing here
should be described as an AWS or CockroachDB Cloud deployment until the
corresponding execution evidence exists.

## 1. First Cloud deployment — blocked only by account setup

- [ ] Create a CockroachDB Cloud cluster with vector indexing enabled.
- [ ] Create distinct CockroachDB service principals for `resolver-prod`,
  `sweeper-prod`, seeder/migration, authority administrator, and every agent.
- [ ] Apply `schema.sql`; perform the trusted bootstrap from
  `AUTHORITY_MODEL.md`; then use audited authority operations to register and
  grant production nodes.
- [ ] Create three AWS Secrets Manager JSON secrets: resolver DSN
  (`{"dsn":"..."}`), sweeper DSN, and resolver ingress token
  (`{"token":"..."}`).
- [ ] Deploy `infra/template.json` with AWS SAM. Attach a reviewed alert target
  to the two CloudWatch alarms rather than leaving them informational.
- [ ] Configure CockroachDB webhook changefeed with the resolver Function URL,
  TLS validation, and `extra_headers` token. Never use
  `insecure_tls_skip_verify` outside a disposable test.

## 2. Evidence required before claiming deployment readiness

- [ ] Run every Lambda item in `tests/INTEGRATION_CHECKLIST.md` against the
  Cloud cluster: authenticated resolver delivery, redelivery no-op, echo
  suppression, malformed input, temporary DB failure, cron backlog, warm
  reconnection, and principal mismatch.
- [ ] Confirm CloudWatch `Errors` alarms fire for an induced resolver failure
  and that the notification reaches an accountable owner.
- [ ] Exercise secret rotation deliberately. Record the warm-Lambda behavior,
  rollback/grace period, and final credential revocation; do not assume secret
  caching makes rotation instantaneous.
- [ ] Capture deployment-specific versions, region/network topology, command
  output, and test date in a new audit round.

## 3. Hackathon demo

- [ ] Choose a synthetic scenario with clearly misplaced memories, competing
  recruitment signals, consensus dilution, migration cooldown, an orphan
  sweep, and a revoked node.
- [ ] Record a sub-three-minute video: shared CockroachDB memory → independent
  agent traces → exact deterministic resolution → per-node audit chains →
  Merkle verification → AWS resolver/sweeper once Cloud evidence exists.
- [ ] Keep the local secure demo explicitly labeled as local; never imply it
  is a Cloud deployment.
- [ ] Update README/HACKATHON submission text only with claims supported by the
  recorded Cloud and demo evidence.

## 4. Engineering after the submission

- [ ] Implement the embedding-dimension/schema startup cross-check listed in
  `KNOWN_LIMITATIONS.md`.
- [ ] Design CAS-serialized region split/merge before implementing either;
  resolve-region reads need a reviewed reshape interaction.
- [ ] Design a retention/archive strategy for permanently growing audit chains
  and Merkle snapshots without violating the no-deletion doctrine.
- [ ] Revisit the CockroachDB `FOR SHARE` privilege boundary. The current model
  protects reviewed runtime operations and documents that a compromised SQL
  credential remains outside the guarantee; do not weaken the lock based on
  intuition — the plain-read alternative was experimentally falsified.

## Reference map

- Authority and residual boundary: `AUTHORITY_MODEL.md`
- AWS/Cockroach deployment contract: `docs/AWS_COCKROACH_DEPLOYMENT_CONTRACT.md`
- SAM scaffold: `infra/README.md`
- Real local authority regressions: `tools/run_authority_integration.sh`
- Secure local demo: `tools/run_secure_demo_local.sh`
- Adversarial findings and falsified vector: `docs/SECURITY_AUDIT_ROUND_2.md`
