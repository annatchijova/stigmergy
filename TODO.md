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

Shot order, measured timings and the honesty beats live in `DEMO_RUNBOOK.md`.
What is verified as of this pass: the 230 pure assertions; `demo/console.html`
running the field to 6/6 in ~40 s with zero page errors; the forgery beat
naming the tampered entry; `tests/fixtures/cross_impl.bundle.json` passing all
six checks **in the browser** (the two canonicalizers agree byte for byte); and
a console-exported bundle verifying under `tools/verify_bundle.py` (the round
trip closes in both directions). What is NOT verified: anything requiring a
cluster or an AWS account — see sections 1 and 2, still unexecuted.

- [x] Choose a synthetic scenario with clearly misplaced memories, competing
  recruitment signals, consensus dilution, and migration cooldown
  (`demo/corpus.py` for the cluster, the console's own corpus for the browser;
  both seed six deliberately misplaced memories). An orphan sweep and a revoked
  node are implemented and pure-tested but are NOT part of either demo run's
  narrative yet — the cron sweeper and `revoke_node` would have to be driven
  explicitly to appear on camera.
- [ ] Drive an orphan sweep and a node revocation inside the recorded run, or
  drop them from the submission text. Right now the text would promise a beat
  the footage does not contain. `demo/run_demo.py` never calls either one
  (verified: zero references to `orphans`/`revoke_node` in the harness). The
  cheap path to the orphan beat: call `bounded_drain` with a short
  `ORPHAN_WINDOW_SECONDS` at the end of a cluster run, then load that bundle
  in the console — its replayer already understands `MEMORIES_ORPHANED` and
  paints the tier change, so the beat costs one harness call, not new UI.
- [ ] Record a sub-three-minute video: shared CockroachDB memory → independent
  agent traces → exact deterministic resolution → per-node audit chains →
  Merkle verification → AWS resolver/sweeper once Cloud evidence exists.
- [ ] Keep the local secure demo explicitly labeled as local; never imply it
  is a Cloud deployment.
- [ ] Update README/HACKATHON submission text only with claims supported by the
  recorded Cloud and demo evidence.
- [x] Custody+taint layer ported from MNEME (`audit/custody.py`,
  `ops/trust.py`, `ops/memories.py` integration, `demo/field_viewer.html`)
  — pure-tested, NOT yet exercised against a live cluster. See item 2's new
  "ops.trust / audit.custody" checklist section below before claiming this
  works end to end.
- [ ] Capture `demo/field_viewer.html` (director mode) as part of the
  submission video — it is the visual for custody-gated recall and the
  taint sweep, and it needs no server (open the file directly). It is a
  staged replay, not a live capture; label it that way in the video too,
  same discipline as the local secure demo above.
- [ ] Fast-follow, not required for submission: port MNEME's `bundle.py` /
  `verify_offline.py` (sealed evidence export + independent offline
  verifier) once custody+taint have Cloud integration-checklist evidence.

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
