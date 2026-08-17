#!/usr/bin/env bash
# One command, one disposable cluster, one real run, one sealed bundle.
#
# Knobs (all optional): STIGMERGY_DEMO_AGENTS, STIGMERGY_DEMO_ROUNDS,
# STIGMERGY_DEMO_PROVIDER (minilm|deterministic — auto-detected by default),
# STIGMERGY_DEMO_BUNDLE (output path), STIGMERGY_TEST_SQL_PORT.
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$repo_dir"
agents="${STIGMERGY_DEMO_AGENTS:-3}"; port="${STIGMERGY_TEST_SQL_PORT:-26449}"; http_port="${STIGMERGY_TEST_HTTP_PORT:-8219}"
bundle="${STIGMERGY_DEMO_BUNDLE:-$repo_dir/run.bundle.json}"

# --- preflight: fail with our words, before starting anything ----------------
command -v cockroach >/dev/null 2>&1 || { cat >&2 <<'EOF'
error: the `cockroach` binary is not on PATH.

This demo runs a real CockroachDB node — it is not simulated, so there is
nothing to fall back to. Install the binary first:

  https://www.cockroachlabs.com/docs/releases/  (v25.2+ required: VECTOR indexes)

  curl -sSL https://binaries.cockroachdb.com/cockroach-v25.2.2.linux-amd64.tgz \
    | tar -xz && sudo cp -i cockroach-v25.2.2.linux-amd64/cockroach /usr/local/bin/
EOF
exit 1; }
python3 -c 'import psycopg' 2>/dev/null || { echo "error: pip install 'psycopg[binary]'" >&2; exit 1; }

# The provider is auto-detected and ANNOUNCED, never guessed silently: under the
# deterministic provider run_demo.py refuses to narrate convergence at all
# (is_semantic=False), which is correct and makes for a confusing video.
if [ -n "${STIGMERGY_DEMO_PROVIDER:-}" ]; then provider="$STIGMERGY_DEMO_PROVIDER"
elif python3 -c 'import sentence_transformers' 2>/dev/null; then provider="minilm"
else provider="deterministic"; fi
echo "[demo] provider: $provider"
if [ "$provider" = "deterministic" ]; then cat >&2 <<'EOF'
[demo] NOTE: the deterministic provider has no semantic property, so the report
[demo] will exercise every mechanism and then REFUSE to narrate convergence.
[demo] For the semantic run (the one worth recording):
[demo]   pip install sentence-transformers && STIGMERGY_DEMO_PROVIDER=minilm tools/run_secure_demo_local.sh
EOF
fi
tmp_dir="$(mktemp -d)"; cleanup(){ kill "${server_pid:-}" 2>/dev/null || true; wait "${server_pid:-}" 2>/dev/null || true; rm -rf "$tmp_dir"; }; trap cleanup EXIT
cockroach start-single-node --insecure --store="$tmp_dir/store" --listen-addr="127.0.0.1:$port" --http-addr="127.0.0.1:$http_port" >"$tmp_dir/cockroach.log" 2>&1 & server_pid=$!
for _ in $(seq 1 30); do cockroach sql --insecure --host="127.0.0.1:$port" --execute='SELECT 1' >/dev/null 2>&1 && break; sleep 1; done
cockroach sql --insecure --host="127.0.0.1:$port" --execute='SELECT 1' >/dev/null
cockroach sql --insecure --host="127.0.0.1:$port" < schema.sql >/dev/null
sql="CREATE USER demo_seeder; CREATE USER demo_resolver;"
for i in $(seq 0 $((agents-1))); do sql+=" CREATE USER demo_agent_$i;"; done
cockroach sql --insecure --host="127.0.0.1:$port" --execute="$sql" >/dev/null
users="demo_seeder,demo_resolver"; for i in $(seq 0 $((agents-1))); do users+=",demo_agent_$i"; done
cockroach sql --insecure --host="127.0.0.1:$port" --database=stigmergy --execute="GRANT ALL ON DATABASE stigmergy TO $users; GRANT ALL ON SCHEMA public TO $users; GRANT ALL ON ALL TABLES IN SCHEMA public TO $users; INSERT INTO authority_administrators VALUES ('demo_seeder'); INSERT INTO agent_nodes (node_id,db_principal) VALUES ('demo-seeder','demo_seeder'),('demo-local-resolver','demo_resolver'); INSERT INTO node_capabilities (node_id,capability) VALUES ('demo-seeder','AUTHORITY_ADMIN'),('demo-seeder','REGION_ADMIN');" >/dev/null
for i in $(seq 0 $((agents-1))); do cockroach sql --insecure --host="127.0.0.1:$port" --database=stigmergy --execute="INSERT INTO agent_nodes (node_id,db_principal) VALUES ('agent-$i','demo_agent_$i');" >/dev/null; done
seed_dsn="postgresql://demo_seeder@127.0.0.1:$port/stigmergy?sslmode=disable"; resolver_dsn="postgresql://demo_resolver@127.0.0.1:$port/stigmergy?sslmode=disable"
PYTHONPATH="$repo_dir" python3 tools/bootstrap_secure_demo.py --dsn "$seed_dsn" --agents "$agents"
cmd=(python3 -m demo.run_demo --dsn "$seed_dsn" --seed-dsn "$seed_dsn" --resolver-dsn "$resolver_dsn" --agents "$agents" --rounds "${STIGMERGY_DEMO_ROUNDS:-5}" --provider "$provider" --local-resolver --bundle "$bundle")
for i in $(seq 0 $((agents-1))); do cmd+=(--agent-dsn "postgresql://demo_agent_$i@127.0.0.1:$port/stigmergy?sslmode=disable"); done
PYTHONPATH="$repo_dir" "${cmd[@]}"

# The bundle is the point: it outlives the cluster this script is about to
# delete, and it is what turns demo/console.html from a simulation into a
# dashboard over a run that actually happened.
PYTHONPATH="$repo_dir" python3 -m tools.verify_bundle "$bundle"
cat <<EOF

[demo] sealed evidence bundle: $bundle
[demo] to view the real run: open demo/console.html -> "Open a sealed bundle" -> pick that file.
[demo] the cluster below is about to be deleted; the bundle is not.
EOF
