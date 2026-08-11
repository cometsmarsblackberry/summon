#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${project_root}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
    echo "Missing .venv. Create it with: python3.12 -m venv .venv" >&2
    exit 1
fi

test_runtime_dir="$(mktemp -d "${TMPDIR:-/tmp}/summon-tests.XXXXXX")"
trap 'rm -rf -- "${test_runtime_dir}"' EXIT

export DATABASE_URL="sqlite+aiosqlite:///${test_runtime_dir}/reserve.db"
export LOG_DIR="${test_runtime_dir}/logs"
export SECRET_KEY="summon-local-test-secret"

cd "${project_root}"
"${python_bin}" -m unittest discover -s tests -v

cd "${project_root}/agent"
go test ./...
