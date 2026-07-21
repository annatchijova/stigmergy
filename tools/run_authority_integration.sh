#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

port="${STIGMERGY_TEST_SQL_PORT:-26409}"
http_port="${STIGMERGY_TEST_HTTP_PORT:-8179}"
tmp_dir="$(mktemp -d)"

cleanup() {
  kill "${server_pid:-}" 2>/dev/null || true
  wait "${server_pid:-}" 2>/dev/null || true
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

cockroach start-single-node --insecure --store="$tmp_dir/store" \
  --listen-addr="127.0.0.1:$port" --http-addr="127.0.0.1:$http_port" \
  >"$tmp_dir/cockroach.log" 2>&1 &
server_pid=$!

for _ in $(seq 1 30); do
  if cockroach sql --insecure --host="127.0.0.1:$port" --execute='SELECT 1' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
cockroach sql --insecure --host="127.0.0.1:$port" --execute='SELECT 1' >/dev/null
cockroach sql --insecure --host="127.0.0.1:$port" < schema.sql >/dev/null
STIGMERGY_TEST_DSN="postgresql://root@127.0.0.1:$port/stigmergy?sslmode=disable" \
  PYTHONPATH="$repo_dir" python3 tests/test_authority_integration.py
STIGMERGY_TEST_DSN="postgresql://root@127.0.0.1:$port/stigmergy?sslmode=disable" \
  PYTHONPATH="$repo_dir" python3 tests/test_lambda_authority_integration.py
