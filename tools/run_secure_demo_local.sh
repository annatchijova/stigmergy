#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$repo_dir"
agents="${STIGMERGY_DEMO_AGENTS:-3}"; port="${STIGMERGY_TEST_SQL_PORT:-26449}"; http_port="${STIGMERGY_TEST_HTTP_PORT:-8219}"
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
cmd=(python3 -m demo.run_demo --dsn "$seed_dsn" --seed-dsn "$seed_dsn" --resolver-dsn "$resolver_dsn" --agents "$agents" --rounds "${STIGMERGY_DEMO_ROUNDS:-5}" --provider deterministic --local-resolver)
for i in $(seq 0 $((agents-1))); do cmd+=(--agent-dsn "postgresql://demo_agent_$i@127.0.0.1:$port/stigmergy?sslmode=disable"); done
PYTHONPATH="$repo_dir" "${cmd[@]}"
