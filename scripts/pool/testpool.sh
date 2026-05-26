#!/bin/bash
# testpool.sh — test-pool dispatcher. Mirrors pool.sh but execs
# scripts/pool/adapters/<adapter>-testpool.sh.

set -euo pipefail
_dir="$(cd "$(dirname "$0")" && pwd)"

if [ -n "${AI_HARNESS_POOL_CONFIG:-}" ]; then
  _cfg="$AI_HARNESS_POOL_CONFIG"
elif [ -f "$_dir/config.local.yaml" ]; then
  _cfg="$_dir/config.local.yaml"
else
  echo "testpool: no config found. Create one with:" >&2
  echo "  cp $_dir/config.example.yaml $_dir/config.local.yaml   # then edit" >&2
  echo "or set \$AI_HARNESS_POOL_CONFIG to point at your config file." >&2
  exit 1
fi
[ -f "$_cfg" ] || { echo "testpool: config not found: $_cfg" >&2; exit 1; }

_adapter=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1])).get('adapter','laravel-docker'))" "$_cfg")
_target="$_dir/adapters/${_adapter}-testpool.sh"
[ -x "$_target" ] || { echo "testpool: adapter not found: $_target" >&2; exit 1; }
exec "$_target" "$@"
