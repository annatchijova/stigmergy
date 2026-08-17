#!/usr/bin/env bash
# Provision the CockroachDB service-account users for the STIGMERGY deploy.
#
# - creates the missing stigmergy_seeder user
# - (re)sets passwords for all three runtime principals so their DSNs are known
# - grants least-privilege table access:
#     SELECT + UPDATE on the authority tables (the SELECT ... FOR SHARE / FOR
#       UPDATE locks in ops/authority.py require the UPDATE privilege), NO INSERT
#       so a runtime principal cannot self-grant capabilities by raw SQL;
#     SELECT + INSERT + UPDATE on the data tables.
#   The STIGMERGY domain layer (node capabilities) remains the real gate; these
#   SQL grants are the coarse layer beneath it.
#
# Run as a CockroachDB admin (anna). Writes the resulting DSNs to a local,
# git-ignored secrets file for gcp/deploy.sh — passwords never enter the repo.
set -euo pipefail

: "${DSN_ANNA:?export DSN_ANNA (admin DSN for user anna, pointing at any db)}"
# No sslrootcert in the DSN: libpq falls back to ~/.postgresql/root.crt, which
# exists locally and is baked into the container image (see gcp/Dockerfile).
# CockroachDB Cloud uses a private CA, so the system trust store does NOT verify
# it — the cluster's own root.crt must be the trust anchor.
HOST_DB="combat-mummy-30255.j77.aws-us-east-1.cockroachlabs.cloud:26257/stigmergy?sslmode=verify-full"
OUT="${SECRETS_OUT:-$HOME/stigmergy-secrets.env}"

# URL-safe passwords (alphanumeric only): no percent-encoding needed in a DSN.
gen() { openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | cut -c1-28; }
RES_PW="$(gen)"; SWP_PW="$(gen)"; SED_PW="$(gen)"

cockroach sql --url "$DSN_ANNA" -e "
CREATE USER IF NOT EXISTS stigmergy_seeder;
ALTER USER stigmergy_resolver WITH PASSWORD '${RES_PW}';
ALTER USER stigmergy_sweeper  WITH PASSWORD '${SWP_PW}';
ALTER USER stigmergy_seeder   WITH PASSWORD '${SED_PW}';

GRANT CONNECT ON DATABASE stigmergy TO stigmergy_resolver, stigmergy_sweeper, stigmergy_seeder;

GRANT SELECT, UPDATE ON TABLE
  stigmergy.authority_administrators, stigmergy.agent_nodes,
  stigmergy.node_capabilities, stigmergy.node_region_capabilities
TO stigmergy_resolver, stigmergy_sweeper, stigmergy_seeder;

GRANT SELECT, INSERT, UPDATE ON TABLE
  stigmergy.memory_regions, stigmergy.memories, stigmergy.cell_links,
  stigmergy.recruitment_signals, stigmergy.agent_search_state,
  stigmergy.audit_chain, stigmergy.merkle_snapshots,
  stigmergy.custody_chain, stigmergy.taint_sweeps
TO stigmergy_resolver, stigmergy_sweeper, stigmergy_seeder;
"

umask 077
cat > "$OUT" <<EOF
# STIGMERGY service DSNs — generated $(date -u +%Y-%m-%dT%H:%M:%SZ). DO NOT COMMIT.
# resolver + sweeper feed gcp/deploy.sh; seeder is for the local seeding job.
export RESOLVER_DSN='postgresql://stigmergy_resolver:${RES_PW}@${HOST_DB}'
export SWEEPER_DSN='postgresql://stigmergy_sweeper:${SWP_PW}@${HOST_DB}'
export SEEDER_DSN='postgresql://stigmergy_seeder:${SED_PW}@${HOST_DB}'
export CHANGEFEED_TOKEN='$(openssl rand -hex 32)'
EOF

echo "OK: seeder created, passwords set, grants applied."
echo "Wrote ${OUT} (mode 600). Before gcp/deploy.sh run:  source ${OUT}"
