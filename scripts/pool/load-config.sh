# Source this from an adapter to load pool config into shell vars.
#
# Resolution order for the config path:
#   1. $AI_HARNESS_POOL_CONFIG (explicit env override)
#   2. <scripts/pool>/config.local.yaml (gitignored, local symlink)
# If neither exists, prints a help line and exits the calling script.

_pool_load_config__dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -n "${AI_HARNESS_POOL_CONFIG:-}" ]; then
  _pool_cfg="$AI_HARNESS_POOL_CONFIG"
elif [ -f "$_pool_load_config__dir/config.local.yaml" ]; then
  _pool_cfg="$_pool_load_config__dir/config.local.yaml"
else
  cat >&2 <<MSG
pool: no config found. Set AI_HARNESS_POOL_CONFIG or create a local config:
  cp $_pool_load_config__dir/config.example.yaml $_pool_load_config__dir/config.local.yaml
  # edit, then re-run
MSG
  exit 1
fi

if [ ! -f "$_pool_cfg" ]; then
  echo "pool: config path does not exist: $_pool_cfg" >&2
  exit 1
fi

eval "$(python3 "$_pool_load_config__dir/_yaml_to_env.py" "$_pool_cfg")"
export AI_HARNESS_POOL_CONFIG="$_pool_cfg"
unset _pool_load_config__dir _pool_cfg
