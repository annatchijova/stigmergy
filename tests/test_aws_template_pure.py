"""Static contract checks for the SAM template; no AWS account is needed."""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    with open(os.path.join(ROOT, "infra", "template.json"), encoding="utf-8") as handle:
        template = json.load(handle)
    assert template["Transform"] == "AWS::Serverless-2016-10-31"
    resources = template["Resources"]
    resolver = resources["ResolverFunction"]["Properties"]
    sweeper = resources["SweeperFunction"]["Properties"]

    assert resolver["Handler"] == "lambdas.changefeed_resolver.handler"
    assert sweeper["Handler"] == "lambdas.cron_sweeper.handler"
    resolver_env = resolver["Environment"]["Variables"]
    sweeper_env = sweeper["Environment"]["Variables"]
    assert set(resolver_env) == {
        "STIGMERGY_NODE_ID",
        "STIGMERGY_DSN_SECRET_ARN",
        "STIGMERGY_CHANGEFEED_TOKEN_SECRET_ARN",
    }
    assert set(sweeper_env) == {"STIGMERGY_NODE_ID", "STIGMERGY_DSN_SECRET_ARN"}
    assert resolver["FunctionUrlConfig"]["AuthType"] == "NONE"
    assert "ScheduledSweep" in sweeper["Events"]
    assert sweeper["Events"]["ScheduledSweep"]["Type"] == "Schedule"

    resolver_policy = resources["ResolverRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    resolver_secret_statement = next(s for s in resolver_policy if "secretsmanager:GetSecretValue" in s["Action"])
    assert resolver_secret_statement["Resource"] == [
        {"Ref": "ResolverDsnSecretArn"},
        {"Ref": "ResolverIngressSecretArn"},
    ]
    sweeper_policy = resources["SweeperRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    sweeper_secret_statement = next(s for s in sweeper_policy if "secretsmanager:GetSecretValue" in s["Action"])
    assert sweeper_secret_statement["Resource"] == {"Ref": "SweeperDsnSecretArn"}
    print("aws template contract: PASS (separate secrets, roles, resolver ingress, schedule)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
