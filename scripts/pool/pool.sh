#!/bin/bash
# pool.sh — dev-preview pool dispatcher. Reads the `adapter` field from the
# config YAML and execs scripts/pool/adapters/<adapter>-pool.sh.
#
# Config resolution: $AI_HARNESS_POOL_CONFIG, else scripts/pool/config.local.yaml.
# See config.example.yaml for the shape; only `laravel-docker` ships.

set -euo pipefail
_dir="$(cd "$(dirname "$0")" && pwd)"

if [ -n "${AI_HARNESS_POOL_CONFIG:-}" ]; then
  _cfg="$AI_HARNESS_POOL_CONFIG"
elif [ -f "$_dir/config.local.yaml" ]; then
  _cfg="$_dir/config.local.yaml"
else
  echo "pool: no config found. Create one with:" >&2
  echo "  cp $_dir/config.example.yaml $_dir/config.local.yaml   # then edit" >&2
  echo "or set \$AI_HARNESS_POOL_CONFIG to point at your config file." >&2
  exit 1
fi
[ -f "$_cfg" ] || { echo "pool: config not found: $_cfg" >&2; exit 1; }

_adapter=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1])).get('adapter','laravel-docker'))" "$_cfg")
_target="$_dir/adapters/${_adapter}-pool.sh"
[ -x "$_target" ] || { echo "pool: adapter not found: $_target" >&2; exit 1; }
exec "$_target" "$@"
