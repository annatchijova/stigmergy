# AWS SAM deployment scaffold

`template.json` is a deployable AWS SAM scaffold, not evidence of an AWS
deployment. It exists so the Cloud phase has a reviewable starting point before
an account is opened.

## What it creates

- A resolver Lambda with a public Function URL. It is protected by the
  application-level CockroachDB changefeed token, not by an open operational
  endpoint.
- An EventBridge-scheduled sweeper Lambda.
- Separate execution roles and separate Secrets Manager read permissions:
  resolver can read its DSN plus ingress-token secret; sweeper can read only
  its DSN secret.

The template does not create CockroachDB users, STIGMERGY authority records,
or a changefeed job. Those are domain-state operations with a deliberate
bootstrap/audit path; see `../docs/AWS_COCKROACH_DEPLOYMENT_CONTRACT.md`.

## Secret shapes

Create JSON secrets, rather than raw strings:

```json
{"dsn": "postgresql://..."}
```

for each node's separate CockroachDB DSN, and:

```json
{"token": "a-high-entropy-changefeed-token"}
```

for the resolver ingress token. The runtime reads secrets lazily with the
function's IAM role and caches them only for the warm process lifetime.

## When an AWS account is ready

1. Build a CockroachDB Cloud cluster and separate SQL/service principals as
   specified by the deployment contract. Apply `schema.sql`, perform the
   trusted bootstrap, then register and grant `resolver-prod` and
   `sweeper-prod` through audited authority operations.
2. Create the three Secrets Manager secrets above. Do not reuse the resolver
   DSN for the sweeper.
3. Install AWS SAM CLI and deploy from repository root:

   ```bash
   sam build --template-file infra/template.json
   sam deploy --guided \
     --parameter-overrides \
       ResolverDsnSecretArn=arn:... \
       ResolverIngressSecretArn=arn:... \
       SweeperDsnSecretArn=arn:...
   ```

4. Invoke the sweeper once. Send a resolver heartbeat using the configured
   header. Both must pass the deployment-identity checks before live traffic.
5. Create the CockroachDB changefeed using the `ResolverFunctionUrl` output
   and its matching `extra_headers` token. Confirm TLS certificate validation;
   do not set `insecure_tls_skip_verify` in a real deployment.

If the CockroachDB endpoint requires private networking, add reviewed VPC,
security-group, and egress configuration before deployment. The template does
not pretend public internet connectivity is a universal production topology.
