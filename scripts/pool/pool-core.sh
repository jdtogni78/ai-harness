# pool-core.sh — project-agnostic library for slot-pool adapters.
#
# The adapter sources this AFTER setting:
#   TABLE      path to the lease TSV (e.g. $STATE_DIR/pool.tsv)
#   LOCK       path to the mkdir mutex dir
#   SLOTS      array of slot names (e.g. (pool0 pool1 ...))
#   OFFSETS    parallel array of offsets (slot index N maps to port 3000+OFFSETS[N])
#   POOL_TAG   short string for error messages (e.g. "pool" or "testpool")
#
# Provides: init_table, lock/unlock, set_row, normalize_table, cmd_list,
# wt_app1, wt_branch, canon_off, owner_of, find_free_slot, find_my_slot,
# reserve_port_file, clear_port_file.
#
# State conventions:
#   leased  — slot held by a worktree; preserved verbatim across normalize.
#   free    — available at the canonical offset.
#   blocked — reserved but its canonical port is held by a (legacy) leased row;
#             self-heals to `free` once that lease releases.

# Canonical offset for a slot name (from SLOTS/OFFSETS), or empty.
canon_off() {
  local s="$1" i
  for i in "${!SLOTS[@]}"; do
    [ "${SLOTS[$i]}" = "$s" ] && { echo "${OFFSETS[$i]}"; return; }
  done
}

# Initialize the lease TSV if it doesn't exist.
init_table() {
  [ -f "$TABLE" ] && return
  mkdir -p "$(dirname "$TABLE")"
  { printf 'slot\toffset\tapp_port\tstatus\tworktree\tbranch\tlabel\tclaimed_at\thost\n'
    for i in "${!SLOTS[@]}"; do
      printf '%s\t%s\t%s\tfree\t-\t-\t-\t-\t-\n' \
        "${SLOTS[$i]}" "${OFFSETS[$i]}" "$((3000 + OFFSETS[$i]))"
    done
  } > "$TABLE"
}

# macOS has no flock; use a mkdir directory as the mutex.
lock() {
  local n=0
  until mkdir "$LOCK" 2>/dev/null; do
    n=$((n+1))
    [ $n -gt 100 ] && { echo "${POOL_TAG:-pool}: lock held; try '$0 gc'" >&2; exit 1; }
    sleep 0.1
  done
}
unlock() { rmdir "$LOCK" 2>/dev/null || true; }

# Replace a row in the table. Slot stays at its canonical offset/port.
set_row() { # slot status worktree branch label claimed_at host
  local s="$1" off port
  off=$(awk -F'\t' -v s="$s" '$1==s{print $2}' "$TABLE")
  port=$((3000 + off))
  awk -F'\t' -v OFS='\t' -v s="$s" -v st="$2" -v wt="$3" -v br="$4" \
      -v lb="$5" -v at="$6" -v ho="$7" -v off="$off" -v port="$port" \
    'NR==1{print;next} $1==s{print s,off,port,st,wt,br,lb,at,ho;next} {print}' \
    "$TABLE" > "$TABLE.tmp" && mv "$TABLE.tmp" "$TABLE"
}

# Re-derive offset/port for non-leased rows from SLOTS/OFFSETS. A free row
# whose canonical port is still held by a (legacy-named) leased row stays
# 'blocked'; otherwise it becomes 'free' at its canonical offset. Leased rows
# are preserved verbatim so an in-flight session's release keeps working.
normalize_table() {
  [ -f "$TABLE" ] || return
  local map="" i
  for i in "${!SLOTS[@]}"; do map+="${SLOTS[$i]}=${OFFSETS[$i]};"; done
  awk -F'\t' -v OFS='\t' -v map="$map" '
    BEGIN{ n=split(map,a,";"); for(j=1;j<=n;j++){ if(a[j]!=""){ split(a[j],kv,"="); CO[kv[1]]=kv[2] } } }
    NR==1{ print; next }
    { row[NR]=$0; slot[NR]=$1; st[NR]=$4; port[NR]=$3; if($4=="leased") LP[$3]=1; last=NR }
    END{
      for(r=2;r<=last;r++){
        $0=row[r];
        s=slot[r]; co=CO[s];
        if(st[r]=="leased" || co==""){ print row[r]; continue }
        cport=3000+co;
        if(cport in LP){ $2=co; $3=cport; $4="blocked" }
        else           { $2=co; $3=cport; $4="free" }
        print
      }
    }' "$TABLE" > "$TABLE.tmp" && mv "$TABLE.tmp" "$TABLE"
}

# Caller must be in a worktree's app1/ directory; echo it.
wt_app1() {
  case "$PWD" in
    */app1) echo "$PWD" ;;
    *) echo "${POOL_TAG:-pool}: run this from a worktree's app1/ directory" >&2; exit 1 ;;
  esac
}
wt_branch() { git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown; }

owner_of()       { awk -F'\t' -v s="$1" '$1==s{print $4}' "$TABLE"; }
find_free_slot() { awk -F'\t' '$4=="free"{print $1; exit}' "$TABLE"; }
find_my_slot()   { awk -F'\t' -v w="$1" '$5==w && $4=="leased"{print $1; exit}' "$TABLE"; }

# Record/remove the slot's offset in <worktree>/.dc-ports so the launcher uses it.
reserve_port_file() { # app1 slot offset
  touch "$1/.dc-ports"
  grep -q "^${2}=" "$1/.dc-ports" 2>/dev/null || echo "${2}=${3}" >> "$1/.dc-ports"
}
clear_port_file() { # app1 slot
  [ -f "$1/.dc-ports" ] || return 0
  sed -i.bak "/^${2}=/d" "$1/.dc-ports" 2>/dev/null && rm -f "$1/.dc-ports.bak" || true
}

cmd_list() {
  init_table
  normalize_table
  column -t -s $'\t' "$TABLE"
}
