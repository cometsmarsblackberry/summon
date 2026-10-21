#!/usr/bin/env bash
# Print the license and notice files of the Go standard library and of every
# module package linked into tf2-agent. Run it with the GOOS, GOARCH, and
# CGO_ENABLED values of the agent build, after the agent's modules are
# downloaded. It fails if a linked module has no license file.
set -Eeuo pipefail

agent_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../agent" && pwd)"
cd "${agent_dir}"

separator="================================================================================"

print_file() {
  printf '%s\n%s\n%s\n\n' "${separator}" "$1" "${separator}"
  cat "$2"
  printf '\n'
}

packages="$(
  go list -deps -f '{{if not .Standard}}{{with .Module}}{{if not .Main}}{{$.Dir}}{{"\t"}}{{.Dir}}{{"\t"}}{{.Path}} {{.Version}}{{end}}{{end}}{{end}}' .
)"

declare -A module_has_license=()
entries=()
while IFS=$'\t' read -r package_dir module_dir module; do
  test -n "${package_dir}" || continue
  module_has_license["${module}"]="${module_has_license["${module}"]:-0}"
  # A package is covered by the license files in its own directory and in each
  # parent directory up to its module root.
  dir="${package_dir}"
  while :; do
    while IFS= read -r file; do
      entries+=("${module}"$'\t'"${file#"${module_dir}/"}"$'\t'"${file}")
      case "${file##*/}" in
        [Nn][Oo][Tt][Ii][Cc][Ee]*) ;;
        *) module_has_license["${module}"]=1 ;;
      esac
    done < <(
      find "${dir}" -maxdepth 1 -type f \
        \( -iname 'licen[cs]e*' -o -iname 'copying*' -o -iname 'notice*' \) | sort
    )
    if [ "${dir}" = "${module_dir}" ]; then
      break
    fi
    dir="$(dirname "${dir}")"
  done
done <<<"${packages}"

go_license="$(go env GOROOT)/LICENSE"
if [ ! -f "${go_license}" ]; then
  echo "Go standard library license not found at ${go_license}" >&2
  exit 1
fi

test "${#module_has_license[@]}" -gt 0
for module in "${!module_has_license[@]}"; do
  if [ "${module_has_license["${module}"]}" != 1 ]; then
    echo "No license file found for linked module ${module}" >&2
    exit 1
  fi
done

cat <<'EOF'
Third-party licenses for tf2-agent

tf2-agent is part of Summon (https://github.com/cometsmarsblackberry/summon),
licensed under AGPL-3.0. It is built from the Go standard library and the Go
modules below, whose license and notice files follow.

EOF

print_file "Go standard library ($(go env GOVERSION)): LICENSE" "${go_license}"

while IFS=$'\t' read -r module relative_path file; do
  print_file "${module}: ${relative_path}" "${file}"
done < <(printf '%s\n' "${entries[@]}" | sort -u)
