"""GCP Cloud Run adapter for the STIGMERGY resolver and sweeper handlers.

The identity model is unchanged from the AWS deployment contract
(docs/AWS_COCKROACH_DEPLOYMENT_CONTRACT.md): one Cloud Run service = one
STIGMERGY node = one CockroachDB principal = one STIGMERGY_NODE_ID. The two
roles run as SEPARATE services with SEPARATE secrets; this module is only the
HTTP transport around the existing, cloud-agnostic handlers, selected by the
STIGMERGY_ROLE environment variable.

Secrets: Cloud Run's ``--set-secrets`` maps a Secret Manager version straight
to STIGMERGY_DSN / STIGMERGY_CHANGEFEED_TOKEN as plain environment variables,
so the handlers' existing direct-env secret path is reused verbatim — no
google-cloud SDK dependency and no change to lambdas/common.py.

Auth posture (mirrors the AWS design):
  - resolver: Cloud Run IAM public (--allow-unauthenticated), because
    CockroachDB's webhook sink cannot present a GCP OIDC token. It is
    protected at the application layer by the constant-time
    x-stigmergy-changefeed-token check inside the handler itself.
  - sweeper: Cloud Run IAM private (--no-allow-unauthenticated); Cloud
    Scheduler invokes it with an OIDC token and the run.invoker role.
"""
from __future__ import annotations

import json
import os
import sys

# The handlers import first-party packages (lambdas/, ops/, audit/); make the
# repository root importable regardless of the process working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, request

app = Flask(__name__)

_VALID_ROLES = ("resolver", "sweeper")


def require_role() -> str:
    role = os.environ.get("STIGMERGY_ROLE", "").strip()
    if role not in _VALID_ROLES:
        raise RuntimeError(
            "STIGMERGY_ROLE is required deployment configuration and must be "
            f"one of {_VALID_ROLES}; refusing to start with {role!r}."
        )
    return role


def _json(payload: dict, status: int) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")


@app.get("/healthz")
def healthz() -> Response:
    # Liveness only. Deliberately does NOT open the database or prove authority:
    # the principal<->node binding is proven per real invocation, never by a
    # probe that a misconfigured deployment could still pass.
    return _json({"ok": True, "role": require_role()}, 200)


@app.post("/")
def dispatch() -> Response:
    role = require_role()
    return _resolve() if role == "resolver" else _sweep()


def _resolve() -> Response:
    from lambdas.changefeed_resolver import ChangefeedBatchFailure, handler

    event = {
        "headers": {key: value for key, value in request.headers.items()},
        "body": request.get_data(as_text=True),
    }
    try:
        result = handler(event)
    except ChangefeedBatchFailure as exc:
        # Unexpected per-record failure: answer 500 so the changefeed sink
        # redelivers and Cloud Run records a request error (the GCP analogue of
        # the AWS/Lambda Errors metric). The message is bounded and secret-free.
        return _json({"error": str(exc)}, 500)
    return Response(
        result.get("body", "{}"),
        status=int(result.get("statusCode", 200)),
        mimetype="application/json",
    )


def _sweep() -> Response:
    from lambdas.cron_sweeper import handler

    result = handler()
    return Response(
        result.get("body", "{}"),
        status=int(result.get("statusCode", 200)),
        mimetype="application/json",
    )


if __name__ == "__main__":
    # Local run only; Cloud Run uses gunicorn (see gcp/Dockerfile).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
