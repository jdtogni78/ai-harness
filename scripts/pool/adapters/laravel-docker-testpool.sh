#!/bin/bash
# Laravel+Docker test-env pool adapter (sibling of laravel-docker-pool.sh).
#
# DB model: every slot gets its OWN database on a pool-owned MariaDB
# (host :$DB_TEST_HOST_PORT, container $DB_TEST_CONTAINER):
# test0->${DB_PREFIX}_test0, test1->${DB_PREFIX}_test1, ... On every `claim` the
# slot's DB is dropped and restored FRESH from a committed baseline backup, so
# each lease starts from an identical clean state and concurrent destructive
# suite runs are fully isolated from each other AND from dev.
#
# The baseline backup is repo-committed (one per checkout/branch). Build it
# with `testpool.sh snapshot [dev|<slot>]`:
#   dev   (default): dump $DB_DEV_CONTAINER:${DB_PREFIX}_dev (a copy of dev)
#   <slot>         : dump that slot's DB (capture a hand-curated state)
# `test_light_skip_data` tables are dumped structure-only so the baseline
# stays light (~270 KB gzipped) without losing any functional use case.
#
# The MariaDB is pool-owned (NOT some fragile per-worktree db-test): its
# datadir lives under $STATE_DIR/testdb so it survives worktree churn,
# matching this pool's "state outside the repo" design. `testpool.sh dbup`
# boots it (auto-run by claim/reseed/snapshot/warm).
#
# State:
#   $STATE_DIR/testpool.tsv      lease table   (user-global)
#   $STATE_DIR/testpool.lock/    mkdir mutex   (user-global)
#   $STATE_DIR/testdb/           pool MariaDB datadir
#   $STATE_DIR/warm/             shared READ-ONLY with pool.sh
#
# Usage (run claim/release/run/reseed from a worktree's app1/ dir):
#   testpool.sh list
#   testpool.sh snapshot [dev|<slot>]    # (re)build the baseline backup
#   testpool.sh claim [label]            # lease + restore slot DB + stand up
#   testpool.sh reseed [slot]            # re-restore a slot's DB from baseline
#   testpool.sh run [slot] [-- args]     # php artisan test in the slot
#   testpool.sh tour [slot] [-- args]    # Dusk UI tour in the slot (auto-claims)
#   testpool.sh release [slot]
#   testpool.sh dbup | dbdown | warm | cool | gc

set -euo pipefail

# Claude/non-interactive shells lack /usr/local/bin on PATH; bare `docker`
# then fails with 127 and (worse) silently no-ops DB/stack steps.
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

_adir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$_adir/load-config.sh"
POOL_TAG=testpool
TABLE="$STATE_DIR/testpool.tsv"
LOCK="$STATE_DIR/testpool.lock"
WARM_DIR="$STATE_DIR/warm"
TEST_DB_DATADIR="$STATE_DIR/testdb"
SLOTS=("${POOLS_TEST_SLOTS[@]}")
OFFSETS=("${POOLS_TEST_OFFSETS[@]}")
source "$_adir/pool-core.sh"

DB_USER=root
slot_db() { echo "${DB_PREFIX}_${1}"; }

# ---- pool-owned MariaDB ----------------------------------------------------

# mariadb:lts grants root/$DB_PASSWORD over TCP (root@'%') but NOT over the
# local socket, so every in-container client call must use -h127.0.0.1.
testdb_up() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$DB_TEST_CONTAINER" \
    && docker exec "$DB_TEST_CONTAINER" mariadb -u"$DB_USER" -p"$DB_PASSWORD" -h127.0.0.1 \
         -e 'SELECT 1' >/dev/null 2>&1
}
mysql_test() { docker exec "$DB_TEST_CONTAINER" mariadb -u"$DB_USER" -p"$DB_PASSWORD" -h127.0.0.1 "$@"; }

cmd_dbup() {
  if testdb_up; then echo "testpool: ${DB_TEST_CONTAINER} already up on :${DB_TEST_HOST_PORT}"; return; fi
  if docker ps -a --format '{{.Names}}' | grep -qx "$DB_TEST_CONTAINER"; then
    echo "testpool: starting existing ${DB_TEST_CONTAINER} ..."
    docker start "$DB_TEST_CONTAINER" >/dev/null
  else
    mkdir -p "$TEST_DB_DATADIR"
    echo "testpool: creating ${DB_TEST_CONTAINER} (${DB_TEST_IMAGE}, datadir ${TEST_DB_DATADIR}, host :${DB_TEST_HOST_PORT}) ..."
    docker run -d --name "$DB_TEST_CONTAINER" --restart unless-stopped \
      -e MARIADB_ROOT_PASSWORD="$DB_PASSWORD" \
      -p "${DB_TEST_HOST_PORT}:3306" \
      -v "$TEST_DB_DATADIR":/var/lib/mysql \
      "$DB_TEST_IMAGE" >/dev/null
  fi
  local n=0
  until testdb_up; do
    n=$((n+1)); [ $n -gt 120 ] && { echo "testpool: ${DB_TEST_CONTAINER} not ready after 120s" >&2; exit 1; }
    sleep 1
  done
  echo "testpool: ${DB_TEST_CONTAINER} ready on host :${DB_TEST_HOST_PORT}"
}
cmd_dbdown() {
  docker stop "$DB_TEST_CONTAINER" >/dev/null 2>&1 \
    && echo "testpool: ${DB_TEST_CONTAINER} stopped (datadir kept)" \
    || echo "testpool: ${DB_TEST_CONTAINER} not running"
}
ensure_testdb() { testdb_up || cmd_dbup; }

# ---- baseline / slot DB lifecycle -----------------------------------------

baseline_for() { echo "$1/$APP_TEST_BASELINE_SUBPATH"; }

ensure_db() {
  local db; db=$(slot_db "$1")
  mysql_test -e "CREATE DATABASE IF NOT EXISTS \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null
}
db_empty() {
  local db cnt; db=$(slot_db "$1")
  cnt=$(mysql_test -N -B -e \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$db';" 2>/dev/null || echo 0)
  [ "${cnt:-0}" -eq 0 ]
}
restore_slot() { # slot baseline_file
  local db; db=$(slot_db "$1")
  [ -f "$2" ] || return 2
  mysql_test -e "DROP DATABASE IF EXISTS \`$db\`; CREATE DATABASE \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  gunzip -c "$2" | docker exec -i "$DB_TEST_CONTAINER" \
    mariadb -u"$DB_USER" -p"$DB_PASSWORD" -h127.0.0.1 "$db"
}
seed_slot() {
  docker exec -e FF_SYNTHETIC_BASELINE=1 "${APP_CONTAINER_PREFIX}-$1" \
    php artisan migrate:fresh --seed --seeder="$APP_TEST_BASELINE_SEEDER" --force
}

# ---- compose + warm cache (read-only from pool.sh's WARM_DIR) -------------

warm_ready() { [ -d "$WARM_DIR/vendor" ] && [ -d "$WARM_DIR/public-build" ]; }
lock_matches() {
  [ -f "$WARM_DIR/composer.lock.sha" ] || return 1
  [ "$(shasum "$1/composer.lock" 2>/dev/null | cut -d' ' -f1)" \
    = "$(cat "$WARM_DIR/composer.lock.sha")" ]
}
ff_image() {
  docker images --format '{{.Repository}}' \
    | grep -m1 "${APP_CONTAINER_PREFIX}.*${APP_CONTAINER_PREFIX}" \
    || echo "${APP_CONTAINER_PREFIX}-${SLOTS[0]}-${APP_CONTAINER_PREFIX}"
}

write_override() { # app1 slot include_warm
  local db; db=$(slot_db "$2")
  local main="${APP_CONTAINER_PREFIX}"
  { echo "# Auto-generated by testpool.sh ($2) -- deleted on release/cool."
    echo "services:"
    echo "  ${main}:"
    echo "    environment:"
    echo "      - DB_HOST=host.docker.internal"
    echo "      - DB_PORT=${DB_TEST_HOST_PORT}"
    echo "      - DB_DATABASE=${db}"
    echo "      - DB_USERNAME=${DB_USER}"
    echo "      - DB_PASSWORD=${DB_PASSWORD}"
    if [ "${3:-0}" = 1 ] && warm_ready; then
      echo "    volumes:"
      echo "      - $WARM_DIR/vendor:/app/vendor"
      echo "      - $WARM_DIR/public-build:/app/public/build"
    fi
  } > "$1/docker-compose.${2}.local.yml"
}

docker_rm_slot_containers() {
  local slot="$1" names=("${APP_CONTAINER_PREFIX}-${slot}")
  local p; for p in "${APP_SIDECAR_CONTAINER_PREFIXES[@]:-}"; do
    names+=("${p}-${slot}")
  done
  docker rm -f "${names[@]}" 2>/dev/null || true
}

# ---- commands --------------------------------------------------------------

cmd_claim() {
  init_table
  local label="${1:-}"
  local app1 branch host slot=""
  app1=$(wt_app1)
  branch=$(wt_branch "$app1")
  host=$(hostname -s 2>/dev/null || echo localhost)

  ensure_testdb

  lock
  slot=$(find_my_slot "$app1")
  [ -z "$slot" ] && slot=$(find_free_slot)
  if [ -z "$slot" ]; then
    unlock
    echo "testpool: all ${#SLOTS[@]} slots leased. 'testpool.sh list' / 'testpool.sh gc'." >&2
    exit 1
  fi
  local off port
  off=$(awk -F'\t' -v s="$slot" '$1==s{print $2}' "$TABLE")
  port=$((3000 + off))
  set_row "$slot" leased "$app1" "$branch" "${label:--}" "$(date -u +%FT%TZ)" "$host"
  unlock

  echo "testpool: leased $slot (offset $off) -> http://localhost:$port for $branch"

  reserve_port_file "$app1" "$slot" "$off"

  # Prepare this slot's DB. Branches that ship a committed dump restore it
  # here (before launch). The synthetic-baseline branch (no dump) -> empty DB
  # now + seed via the seeder AFTER the app container is up.
  local bl; bl=$(baseline_for "$app1")
  if [ -f "$bl" ]; then
    echo "testpool: restoring $(slot_db "$slot") fresh from committed baseline ($(du -h "$bl" | cut -f1)) ..."
    restore_slot "$slot" "$bl" \
      && echo "testpool: $(slot_db "$slot") restored." \
      || echo "testpool: WARNING - restore failed" >&2
  else
    ensure_db "$slot" || true
    echo "testpool: no committed dump -> will seed $(slot_db "$slot") via $APP_TEST_BASELINE_SEEDER after launch."
  fi

  local fa="$app1/$APP_SUBDIR" use_warm=0
  if warm_ready && lock_matches "$fa"; then
    use_warm=1
    echo "testpool: composer.lock matches warm cache -> mounting shared vendor/build (no install)"
  fi
  write_override "$app1" "$slot" "$use_warm"

  if [ $use_warm -eq 0 ]; then
    warm_ready || echo "testpool: not warmed (run 'pool.sh warm') -> per-worktree bootstrap"
    if [ ! -d "$fa/vendor" ]; then
      echo "testpool: vendor/ missing -> composer install (one-off container, ~minutes)"
      docker run --rm --entrypoint sh -v "$fa":/app -w /app "$(ff_image):latest" \
        -c "composer install --no-interaction --prefer-dist" \
        || echo "testpool: composer install failed (run 'pool.sh warm' first)" >&2
    fi
    if [ ! -d "$fa/public/build" ] && [ -d "$APP_MAIN_DIR/$APP_SUBDIR/public/build" ]; then
      cp -R "$APP_MAIN_DIR/$APP_SUBDIR/public/build" "$fa/public/"
    fi
  fi
  if [ ! -e "$fa/.env" ]; then
    if [ -f "$fa/.env.dev" ]; then
      # Reuse dev APP_KEY so dev-cloned encrypted columns decrypt.
      sed 's/^DB_HOST=.*/DB_HOST=host.docker.internal/' "$fa/.env.dev" > "$fa/.env.${slot}"
      ( cd "$fa" && ln -sf ".env.${slot}" .env )
    else
      echo "testpool: WARNING - no $fa/.env or .env.dev; app may fail to boot (no APP_KEY)" >&2
    fi
  fi

  echo "testpool: launching $slot stack from $app1 ..."
  ( cd "$app1" && "./${APP_LAUNCH_SCRIPT}" "$slot" \
      -f "docker-compose.${slot}.local.yml" \
      up -d --no-deps --build "${APP_COMPOSE_SERVICES[@]}" )
  ( cd "$app1" && docker exec "${APP_CONTAINER_PREFIX}-${slot}" \
      php artisan config:clear >/dev/null 2>&1 || true )

  if [ ! -f "$bl" ]; then
    echo "testpool: seeding $(slot_db "$slot") via $APP_TEST_BASELINE_SEEDER (migrate:fresh) ..."
    seed_slot "$slot" \
      && echo "testpool: $(slot_db "$slot") seeded." \
      || echo "testpool: WARNING - seed failed" >&2
  fi

  local seed_hint="EMPTY"
  if ! db_empty "$slot"; then
    [ -f "$bl" ] && seed_hint="fresh from committed baseline" || seed_hint="fresh from $APP_TEST_BASELINE_SEEDER"
  fi
  cat <<MSG

testpool: $slot is up.  DB $(slot_db "$slot") on ${DB_TEST_CONTAINER}: $seed_hint
  Re-restore:       testpool.sh reseed $slot          (slot DB <- committed baseline)
  Rebuild baseline: testpool.sh snapshot [dev|$slot]  (then git add + commit it)
  Run suite:        testpool.sh run $slot -- --exclude-group=incomplete,needs-data-refactor
  App (optional):   http://localhost:$port
  Free:             testpool.sh release $slot
MSG
}

# (Re)build the COMMITTED baseline backup for THIS worktree's checkout.
cmd_snapshot() {
  local app1; app1=$(wt_app1)
  local src="${1:-dev}" ctr db
  case "$src" in
    dev)  ctr="$DB_DEV_CONTAINER";  db="${DB_PREFIX}_dev"
          docker ps --format '{{.Names}}' | grep -qx "$ctr" \
            || { echo "testpool: ${ctr} not running (dev stack up?)" >&2; exit 1; } ;;
    test*) canon_off "$src" >/dev/null || { echo "testpool: unknown slot '$src'" >&2; exit 1; }
          ensure_testdb; ctr="$DB_TEST_CONTAINER"; db=$(slot_db "$src") ;;
    *) echo "testpool: snapshot source must be 'dev' or a slot (test0..)" >&2; exit 1 ;;
  esac

  # mariadb-dump in the dev image uses the socket (works there); the test DB
  # needs TCP. Pick host accordingly.
  local hopt=""; [ "$ctr" = "$DB_TEST_CONTAINER" ] && hopt="-h127.0.0.1"
  # Filter structure-only list to tables that ACTUALLY exist. Missing names
  # would abort the second mariadb-dump and silently drop ALL structure-only
  # CREATEs, breaking the restored baseline.
  local existing_raw light_existing=() t
  existing_raw=$(docker exec "$ctr" mariadb -u"$DB_USER" -p"$DB_PASSWORD" $hopt -N -B \
    -e "SELECT table_name FROM information_schema.tables WHERE table_schema='${db}';" 2>/dev/null)
  for t in "${TEST_LIGHT_SKIP_DATA[@]}"; do
    printf '%s\n' "$existing_raw" | grep -qxF "$t" && light_existing+=("$t")
  done

  local ig=()
  for t in "${light_existing[@]}"; do ig+=(--ignore-table="${db}.${t}"); done

  local bl; bl=$(baseline_for "$app1")
  echo "testpool: snapshotting $ctr:$db -> ${bl#$app1/}"
  echo "          (structure-only for: ${light_existing[*]:-<none present>})"
  mkdir -p "$(dirname "$bl")"
  {
    docker exec "$ctr" mariadb-dump -u"$DB_USER" -p"$DB_PASSWORD" $hopt \
      --single-transaction --quick --no-tablespaces "${ig[@]}" "$db"
    if [ "${#light_existing[@]}" -gt 0 ]; then
      docker exec "$ctr" mariadb-dump -u"$DB_USER" -p"$DB_PASSWORD" $hopt \
        --single-transaction --no-tablespaces --no-data --skip-add-drop-table \
        "$db" "${light_existing[@]}" 2>/dev/null || true
    fi
  } | gzip > "$bl.tmp" && mv "$bl.tmp" "$bl"
  echo "testpool: baseline written ($(du -h "$bl" | cut -f1) gzipped) from $src."
  echo "testpool: INTENTIONAL artifact -- commit it:"
  echo "          git -C $app1 add ${APP_TEST_BASELINE_SUBPATH#$APP_SUBDIR/}  # (from $APP_SUBDIR/)"
  echo "          git commit -m 'chore(test): refresh test-baseline'"
}

cmd_reseed() {
  init_table
  local slot="${1:-}" app1
  app1=$(wt_app1)
  if [ -z "$slot" ]; then
    slot=$(find_my_slot "$app1")
    [ -z "$slot" ] && { echo "testpool: this worktree owns no slot (pass a slot name)"; exit 1; }
  fi
  canon_off "$slot" >/dev/null || { echo "testpool: unknown slot '$slot'" >&2; exit 1; }
  ensure_testdb
  local bl; bl=$(baseline_for "$app1")
  if [ -f "$bl" ]; then
    echo "testpool: restoring $(slot_db "$slot") <- committed baseline (drop + load) ..."
    restore_slot "$slot" "$bl"
    echo "testpool: $(slot_db "$slot") restored fresh from baseline."
  else
    echo "testpool: re-seeding $(slot_db "$slot") via $APP_TEST_BASELINE_SEEDER (migrate:fresh) ..."
    seed_slot "$slot"
    echo "testpool: $(slot_db "$slot") re-seeded."
  fi
}

cmd_run() {
  init_table
  local slot="" app1
  if [ $# -gt 0 ] && [ "$1" != "--" ]; then slot="$1"; shift; fi
  [ "${1:-}" = "--" ] && shift
  if [ -z "$slot" ]; then
    app1=$(wt_app1)
    slot=$(find_my_slot "$app1")
    [ -z "$slot" ] && { echo "testpool: this worktree owns no slot (pass a slot name)"; exit 1; }
  fi
  docker ps --format '{{.Names}}' | grep -qx "${APP_CONTAINER_PREFIX}-${slot}" \
    || { echo "testpool: ${APP_CONTAINER_PREFIX}-${slot} not running (claim it first)" >&2; exit 1; }
  local args=("$@"); [ ${#args[@]} -eq 0 ] && args=(--exclude-group=incomplete,needs-data-refactor)
  echo "testpool: php artisan test ${args[*]}  (in ${APP_CONTAINER_PREFIX}-${slot}, DB $(slot_db "$slot"))"
  docker exec "${APP_CONTAINER_PREFIX}-${slot}" sh -c "cd /app && php artisan test ${args[*]}"
}

# Run Laravel Dusk UI tour against an ISOLATED slot DB. The app image has no
# browser, so a Selenium+Chromium sidecar is stood up on the slot's compose
# network and `php artisan dusk` is pointed at it via DUSK_DRIVER_URL.
# LARAVEL_SAIL=1 makes DuskTestCase skip its local chromedriver.
cmd_tour() {
  init_table
  local slot="" app1
  if [ $# -gt 0 ] && [ "$1" != "--" ]; then slot="$1"; shift; fi
  [ "${1:-}" = "--" ] && shift
  app1=$(wt_app1)
  if [ -z "$slot" ]; then
    slot=$(find_my_slot "$app1")
    if [ -z "$slot" ]; then
      echo "testpool: this worktree owns no slot -> claiming one for the tour ..."
      cmd_claim "ui-tour"
      slot=$(find_my_slot "$app1")
      [ -z "$slot" ] && { echo "testpool: claim failed; cannot run tour" >&2; exit 1; }
    fi
  fi
  docker ps --format '{{.Names}}' | grep -qx "${APP_CONTAINER_PREFIX}-${slot}" \
    || { echo "testpool: ${APP_CONTAINER_PREFIX}-${slot} not running (claim it first)" >&2; exit 1; }

  local args=("$@"); [ ${#args[@]} -eq 0 ] && args=(tests/Browser/)

  local net="${APP_CONTAINER_PREFIX}-${slot}_default"
  local chrome="dusk-chrome-${slot}"
  docker rm -f "$chrome" >/dev/null 2>&1 || true
  echo "testpool: starting $chrome ($SELENIUM_IMAGE) on $net ..."
  docker run -d --rm --name "$chrome" --network "$net" --shm-size=2g \
    "$SELENIUM_IMAGE" >/dev/null
  trap 'docker rm -f "'"$chrome"'" >/dev/null 2>&1 || true' EXIT

  local n=0
  until docker exec "${APP_CONTAINER_PREFIX}-${slot}" sh -c \
      "curl -sf http://${chrome}:4444/status 2>/dev/null | grep -q '\"ready\": *true'"; do
    n=$((n+1)); [ $n -gt 90 ] && { echo "testpool: $chrome not ready after 90s" >&2; exit 1; }
    sleep 1
  done

  echo "testpool: php artisan dusk ${args[*]}  (in ${APP_CONTAINER_PREFIX}-${slot}, DB $(slot_db "$slot"), browser $chrome)"
  local rc=0
  docker exec \
    -e LARAVEL_SAIL=1 \
    -e DUSK_DRIVER_URL="http://${chrome}:4444" \
    -e APP_URL="http://${APP_CONTAINER_PREFIX}-${slot}:8000" \
    "${APP_CONTAINER_PREFIX}-${slot}" sh -c "cd /app && php artisan dusk ${args[*]}" || rc=$?
  echo "testpool: screenshots -> ${app1}/$APP_SUBDIR/tests/Browser/screenshots/ (tour/*.png)"
  return $rc
}

cmd_release() {
  init_table
  local slot="${1:-}" app1 row
  if [ -z "$slot" ]; then
    app1=$(wt_app1)
    slot=$(find_my_slot "$app1")
    [ -z "$slot" ] && { echo "testpool: this worktree owns no slot (pass a slot name)"; exit 0; }
  fi
  row=$(awk -F'\t' -v s="$slot" '$1==s{print}' "$TABLE")
  [ -z "$row" ] && { echo "testpool: unknown slot '$slot'" >&2; exit 1; }
  app1=$(echo "$row" | cut -f5)

  if [ -d "$app1" ]; then
    echo "testpool: tearing down $slot stack ..."
    ( cd "$app1" && "./${APP_LAUNCH_SCRIPT}" "$slot" \
        -f "docker-compose.${slot}.local.yml" down ) 2>/dev/null || true
    rm -f "$app1/docker-compose.${slot}.local.yml" "$app1/$APP_SUBDIR/.env.${slot}"
    clear_port_file "$app1" "$slot"
  else
    echo "testpool: worktree $app1 gone; removing containers by name"
    docker_rm_slot_containers "$slot"
  fi
  # The slot's DB is left on the test container (cheap, handy for post-mortems);
  # the next claim/reseed drops + restores it fresh from the baseline.
  lock; set_row "$slot" free - - - - -; normalize_table; unlock
  echo "testpool: $slot freed ($(slot_db "$slot") kept on ${DB_TEST_CONTAINER}; next claim restores it fresh)."
}

cmd_gc() {
  init_table
  local freed=0
  while IFS=$'\t' read -r slot off port status wt rest; do
    [ "$status" = "leased" ] || continue
    if [ ! -d "$wt" ] || ! docker ps -a --format '{{.Names}}' \
         | grep -qx "${APP_CONTAINER_PREFIX}-${slot}"; then
      echo "testpool: reclaiming stale $slot (worktree gone or container absent)"
      [ -d "$wt" ] && rm -f "$wt/docker-compose.${slot}.local.yml" || true
      docker_rm_slot_containers "$slot"
      lock; set_row "$slot" free - - - - -; unlock
      freed=$((freed+1))
    fi
  done < <(tail -n +2 "$TABLE")
  lock; normalize_table; unlock
  echo "testpool: gc done ($freed reclaimed)."
}

cmd_warm() {
  init_table
  warm_ready || { echo "testpool: warm cache absent -- run 'pool.sh warm' first" >&2; exit 1; }
  ensure_testdb
  touch "$APP_MAIN_DIR/.dc-ports"
  local i
  for i in "${!SLOTS[@]}"; do
    local slot="${SLOTS[$i]}" off="${OFFSETS[$i]}" port=$((3000 + OFFSETS[$i]))
    local owner; owner=$(owner_of "$slot")
    [ "$owner" = leased ]  && { echo "testpool: $slot LEASED -- skipping"; continue; }
    [ "$owner" = blocked ] && { echo "testpool: $slot BLOCKED -- skipping"; continue; }
    grep -q "^${slot}=" "$APP_MAIN_DIR/.dc-ports" 2>/dev/null \
      || echo "${slot}=${off}" >> "$APP_MAIN_DIR/.dc-ports"
    ensure_db "$slot" || true
    write_override "$APP_MAIN_DIR" "$slot" 1
    echo "testpool: starting $slot ..."
    ( cd "$APP_MAIN_DIR" && "./${APP_LAUNCH_SCRIPT}" "$slot" -f "docker-compose.${slot}.local.yml" \
        up -d --no-deps --build "${APP_COMPOSE_SERVICES[@]}" )
    ( cd "$APP_MAIN_DIR" && docker exec "${APP_CONTAINER_PREFIX}-${slot}" \
        php artisan optimize >/dev/null 2>&1 || true )
    echo "testpool: $slot ready -> http://localhost:$port"
  done
  echo "testpool: warm complete."
}

cmd_cool() {
  init_table
  local slot owner
  for slot in "${SLOTS[@]}"; do
    owner=$(owner_of "$slot")
    [ "$owner" = leased ]  && { echo "testpool: $slot LEASED -- skipping (use release)"; continue; }
    [ "$owner" = blocked ] && { echo "testpool: $slot BLOCKED -- skipping"; continue; }
    ( cd "$APP_MAIN_DIR" && "./${APP_LAUNCH_SCRIPT}" "$slot" -f "docker-compose.${slot}.local.yml" down ) 2>/dev/null || true
    rm -f "$APP_MAIN_DIR/docker-compose.${slot}.local.yml"
    clear_port_file "$APP_MAIN_DIR" "$slot"
  done
  echo "testpool: cooled (free slots down; leased untouched; test schemas kept)."
}

case "${1:-list}" in
  list)     cmd_list ;;
  claim)    shift; cmd_claim "$@" ;;
  snapshot) shift; cmd_snapshot "$@" ;;
  reseed)   shift; cmd_reseed "$@" ;;
  run)      shift; cmd_run "$@" ;;
  tour)     shift; cmd_tour "$@" ;;
  release)  shift; cmd_release "$@" ;;
  dbup)     cmd_dbup ;;
  dbdown)   cmd_dbdown ;;
  warm)     cmd_warm ;;
  cool)     cmd_cool ;;
  gc)       cmd_gc ;;
  *) echo "usage: testpool.sh {list|claim [label]|snapshot [dev|<slot>]|reseed [slot]|run [slot] [-- args]|tour [slot] [-- args]|release [slot]|dbup|dbdown|warm|cool|gc}" >&2; exit 1 ;;
esac
