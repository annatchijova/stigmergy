#!/usr/bin/env bash
# GCP Cloud Run deployment for the STIGMERGY resolver and sweeper.
#
# Preserves the deployment contract's identity boundary
# (docs/AWS_COCKROACH_DEPLOYMENT_CONTRACT.md) on GCP:
#   - two Cloud Run services, one per node (resolver-prod / sweeper-prod)
#   - a distinct runtime service account per service
#   - a distinct Secret Manager DSN per service; only its service may read it
#   - resolver public at the IAM layer but gated by the app-level changefeed
#     token; sweeper private, invoked by Cloud Scheduler via OIDC
#
# This deploys COMPUTE only and never touches the database. Apply schema.sql,
# run tools/bootstrap_prod_authority.py, and create the CockroachDB
# service-account users (stigmergy_resolver / stigmergy_sweeper) FIRST.
set -euo pipefail

# ---- CONFIG ----------------------------------------------------------------
PROJECT="${GCP_PROJECT:?export GCP_PROJECT=<your-gcp-project-id>}"
REGION="${GCP_REGION:-us-east1}"    # compute region; the cluster stays on AWS us-east-1 (cross-cloud is fine)
REPO="${AR_REPO:-stigmergy}"        # Artifact Registry repository name
TAG="$(git rev-parse --short HEAD 2>/dev/null || echo latest)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/stigmergy-adapter:${TAG}"

# Secret VALUES arrive from the environment, never hardcoded in the repo:
#   export RESOLVER_DSN='postgresql://stigmergy_resolver:...@combat-mummy-...:26257/stigmergy?sslmode=verify-full'
#   export SWEEPER_DSN='postgresql://stigmergy_sweeper:...@combat-mummy-...:26257/stigmergy?sslmode=verify-full'
#   export CHANGEFEED_TOKEN="$(openssl rand -hex 32)"
: "${RESOLVER_DSN:?export RESOLVER_DSN (DSN for the stigmergy_resolver principal)}"
: "${SWEEPER_DSN:?export SWEEPER_DSN (DSN for the stigmergy_sweeper principal)}"
: "${CHANGEFEED_TOKEN:?export CHANGEFEED_TOKEN (high-entropy changefeed ingress token)}"
# ---------------------------------------------------------------------------

gcloud config set project "$PROJECT"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com

# Artifact Registry repo (idempotent).
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker --location "$REGION"

# ---- secrets --------------------------------------------------------------
put_secret() {  # name value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=-
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic
  fi
}
put_secret stigmergy-resolver-dsn     "$RESOLVER_DSN"
put_secret stigmergy-sweeper-dsn      "$SWEEPER_DSN"
put_secret stigmergy-changefeed-token "$CHANGEFEED_TOKEN"

# ---- per-service runtime service accounts ---------------------------------
ensure_sa() {  # id display
  gcloud iam service-accounts describe "$1@${PROJECT}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$1" --display-name "$2"
}
ensure_sa stigmergy-resolver-run "STIGMERGY resolver Cloud Run"
ensure_sa stigmergy-sweeper-run  "STIGMERGY sweeper Cloud Run"
ensure_sa stigmergy-scheduler    "STIGMERGY Cloud Scheduler invoker"
RESOLVER_SA="stigmergy-resolver-run@${PROJECT}.iam.gserviceaccount.com"
SWEEPER_SA="stigmergy-sweeper-run@${PROJECT}.iam.gserviceaccount.com"
SCHED_SA="stigmergy-scheduler@${PROJECT}.iam.gserviceaccount.com"

# Least-privilege secret reads: resolver reads its DSN + the token; sweeper
# reads ONLY its DSN (mirrors the AWS Secrets Manager permission split).
grant_secret() {  # secret member
  gcloud secrets add-iam-policy-binding "$1" \
    --member "serviceAccount:$2" --role roles/secretmanager.secretAccessor >/dev/null
}
grant_secret stigmergy-resolver-dsn     "$RESOLVER_SA"
grant_secret stigmergy-changefeed-token "$RESOLVER_SA"
grant_secret stigmergy-sweeper-dsn      "$SWEEPER_SA"

# ---- build the image ------------------------------------------------------
gcloud builds submit --config gcp/cloudbuild.yaml --substitutions "_IMAGE=${IMAGE}" .

# ---- deploy resolver (public IAM, app-token gated) ------------------------
gcloud run deploy stigmergy-resolver \
  --image "$IMAGE" --region "$REGION" \
  --service-account "$RESOLVER_SA" \
  --allow-unauthenticated \
  --set-env-vars "STIGMERGY_ROLE=resolver,STIGMERGY_NODE_ID=resolver-prod" \
  --set-secrets "STIGMERGY_DSN=stigmergy-resolver-dsn:latest,STIGMERGY_CHANGEFEED_TOKEN=stigmergy-changefeed-token:latest"

# ---- deploy sweeper (private IAM, Scheduler-invoked) ----------------------
gcloud run deploy stigmergy-sweeper \
  --image "$IMAGE" --region "$REGION" \
  --service-account "$SWEEPER_SA" \
  --no-allow-unauthenticated \
  --set-env-vars "STIGMERGY_ROLE=sweeper,STIGMERGY_NODE_ID=sweeper-prod" \
  --set-secrets "STIGMERGY_DSN=stigmergy-sweeper-dsn:latest"

RESOLVER_URL="$(gcloud run services describe stigmergy-resolver --region "$REGION" --format='value(status.url)')"
SWEEPER_URL="$(gcloud run services describe stigmergy-sweeper  --region "$REGION" --format='value(status.url)')"

# Scheduler SA may invoke the private sweeper.
gcloud run services add-iam-policy-binding stigmergy-sweeper --region "$REGION" \
  --member "serviceAccount:$SCHED_SA" --role roles/run.invoker >/dev/null

# ---- Cloud Scheduler: sweeper tick (replaces EventBridge) -----------------
if gcloud scheduler jobs describe stigmergy-sweeper-tick --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http stigmergy-sweeper-tick --location "$REGION" \
    --schedule "*/5 * * * *" --uri "${SWEEPER_URL}/" --http-method POST \
    --oidc-service-account-email "$SCHED_SA" --oidc-token-audience "$SWEEPER_URL"
else
  gcloud scheduler jobs create http stigmergy-sweeper-tick --location "$REGION" \
    --schedule "*/5 * * * *" --uri "${SWEEPER_URL}/" --http-method POST \
    --oidc-service-account-email "$SCHED_SA" --oidc-token-audience "$SWEEPER_URL"
fi

cat <<EOF

Deployed.
  resolver URL : ${RESOLVER_URL}   (public IAM; gated by x-stigmergy-changefeed-token)
  sweeper URL  : ${SWEEPER_URL}    (private; Cloud Scheduler invokes it every 5 min)

Smoke-test the identity binding before wiring live traffic:
  # sweeper (needs an OIDC token from a run.invoker principal):
  curl -s -X POST "${SWEEPER_URL}/" -H "Authorization: Bearer \$(gcloud auth print-identity-token)"
  # resolver heartbeat (public URL, but must carry the app token):
  curl -s -X POST "${RESOLVER_URL}/" \\
    -H "x-stigmergy-changefeed-token: \${CHANGEFEED_TOKEN}" \\
    -H "content-type: application/json" -d '{"payload":[],"length":0}'

Then, on the CockroachDB cluster, create the changefeed pointing at the resolver:
  CREATE CHANGEFEED FOR TABLE recruitment_signals
    INTO 'webhook-${RESOLVER_URL}'
    WITH updated, resolved = '30s',
         extra_headers = '{"x-stigmergy-changefeed-token":"<CHANGEFEED_TOKEN>"}';

Note: the webhook changefeed sink needs an enterprise/CockroachDB Cloud cluster
with rangefeeds enabled. If this Serverless tier refuses it, drive the resolver
with a polling stand-in (as the local demo does) and label it where it appears.
EOF
