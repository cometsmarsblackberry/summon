#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$script_dir/.." && pwd)
compose_file="$repo_dir/docker-compose.preview.yml"
preview_port=${SUMMON_PREVIEW_PORT:-8000}

cleanup() {
    docker compose -f "$compose_file" down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

printf 'Starting the disposable Summon UI preview...\n'
printf 'Open http://127.0.0.1:%s/__dev/login\n\n' "$preview_port"

docker compose -f "$compose_file" up --build
