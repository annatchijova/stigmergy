# GCP Cloud Run deployment

The Google Cloud counterpart to `infra/` (AWS SAM). Same identity model, same
authority contract (`docs/AWS_COCKROACH_DEPLOYMENT_CONTRACT.md`) — only the
compute substrate changes. This directory is a deployable adapter, not evidence
of a deployment; say "deployed" only after `deploy.sh` has run and the identity
smoke-tests pass.

## Why this is thin

The domain core (`ops/`, `audit/`) and the handlers (`lambdas/`) are
cloud-agnostic. AWS coupling lived in exactly three places, and only the first
two matter here:

| AWS | GCP | change needed |
| --- | --- | --- |
| Lambda + Function URL | Cloud Run service | `main.py` HTTP wrapper (this dir) |
| EventBridge schedule | Cloud Scheduler (OIDC) | `deploy.sh` |
| Secrets Manager (`boto3`) | Secret Manager via `--set-secrets` | none — reuses the handlers' direct-env secret path |

`main.py` is the whole adapter: it shapes the HTTP request into the `event`
dict the existing `handler()` expects and maps the result back. No handler,
`ops/`, or `audit/` code is modified.

## Identity and auth (unchanged contract)

- Two SEPARATE services: `stigmergy-resolver` (node `resolver-prod`, principal
  `stigmergy_resolver`) and `stigmergy-sweeper` (node `sweeper-prod`, principal
  `stigmergy_sweeper`). Distinct runtime service accounts, distinct DSN secrets.
- resolver: Cloud Run IAM **public**, because CockroachDB's webhook cannot
  present a GCP OIDC token — protected instead by the constant-time
  `x-stigmergy-changefeed-token` check inside the handler.
- sweeper: Cloud Run IAM **private**; Cloud Scheduler invokes it with OIDC and
  `roles/run.invoker`.

## Order of operations

1. `schema.sql` applied, `tools/bootstrap_prod_authority.py` run, and the
   CockroachDB users `stigmergy_resolver` / `stigmergy_sweeper` created with
   their least-privilege grants. **The database side comes first.**
2. Export the three secret values and the project:

   ```bash
   export GCP_PROJECT=your-project-id
   export RESOLVER_DSN='postgresql://stigmergy_resolver:...@combat-mummy-...:26257/stigmergy?sslmode=verify-full'
   export SWEEPER_DSN='postgresql://stigmergy_sweeper:...@combat-mummy-...:26257/stigmergy?sslmode=verify-full'
   export CHANGEFEED_TOKEN="$(openssl rand -hex 32)"
   bash gcp/deploy.sh
   ```

3. Run the two identity smoke-tests printed at the end, then create the
   changefeed against the resolver URL.

## Residual boundary

The webhook changefeed sink needs an enterprise / CockroachDB Cloud cluster
with rangefeeds enabled. If the Serverless tier refuses it, the resolver still
works when driven by a polling stand-in (as the local demo does); label that
substitution wherever it appears, per the demo runbook's honesty rule. Nothing
here defends against a stolen service-account secret or a CockroachDB
superuser — those remain the contract's explicit residual trust boundaries.
