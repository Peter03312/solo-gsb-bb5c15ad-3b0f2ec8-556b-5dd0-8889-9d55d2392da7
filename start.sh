#!/bin/sh
# 容器启动脚本：初始化数据（仅首次）并以 waitress 启动 HTTP 服务。
set -e

export PARK_DB_PATH="${PARK_DB_PATH:-/data/park.db}"
export PARK_HOST="${PARK_HOST:-0.0.0.0}"
export PARK_PORT="${PARK_PORT:-8080}"
export PARK_SEED_GRAPH="${PARK_SEED_GRAPH:-/app/seed/graph.json}"
export PARK_SEED_CLOSURES="${PARK_SEED_CLOSURES:-/app/seed/closures.json}"

python -m app.init_data

exec waitress-serve \
  --host="${PARK_HOST}" \
  --port="${PARK_PORT}" \
  --threads="${PARK_THREADS:-8}" \
  wsgi:app
