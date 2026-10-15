#!/usr/bin/env bash
set -euo pipefail

REPOS_DIR="${REPOS_DIR:-/data/repos}"
GITNEXUS_HOME="${GITNEXUS_HOME:-/data/gitnexus}"
export GITNEXUS_HOME

# slug|github_org/repo
DEFAULT_REPOS=(
  "hlt-gpt-researcher|Awhitter/hlt-gpt-researcher"
  "mmm2|Awhitter/MMM2"
  "katailyst2|Awhitter/katailyst2"
  "ebb|Awhitter/evidence-based-business"
  "scrapervault|Awhitter/ScraperVault"
  "nursing-mastery|Awhitter/nursing-mastery"
  "hlt-web-service|HLT-Master/hlt-web-service"
)

TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
REPOS_SPEC="${CODEGRAPH_REPOS:-}"

# NOTE: do NOT add a --max-old-space-size cap here expecting it to prevent the
# OOM. The service already runs with NODE_OPTIONS=--max-old-space-size=640 and
# the container still died using OVER 2GB (2026-08-06), so the memory is NOT in
# V8's old space — it is native: tree-sitter parsers, SQLite, and gitnexus'
# worker processes. A heap cap cannot bound those, and adding one here is worse
# than useless because it reads like a fix.
#
# What actually stops the outage is the freshness guard below: the OOM kills the
# whole container (indexer and MCP server share it), Render restarts, and the
# boot reindex used to immediately re-run the work that caused the OOM.
#
# And the indexer CANNOT be moved to a cron job, which is the obvious next idea:
# Render disks are "accessible by only a single service instance" and "you can't
# add a disk to a cron job service", so a separate service has nowhere to write
# the index this one serves. Indexing has to live here, which means the only
# levers are the size of the work and the size of the plan.

# Repos to leave alone entirely. A repo whose FULL rebuild does not fit in the
# container is unrecoverable once gitnexus sets its incrementalInProgress flag:
# every later run is forced down the full-rebuild path and OOMs the container,
# taking the MCP server with it. Listing it here keeps its last good index on
# disk and served, at the cost of that index going stale — a stale answer beats
# a daily outage. Remove the entry once the plan can hold the rebuild.
SKIP_REPOS="${CODEGRAPH_SKIP_REPOS:-}"
SOURCE_ONLY_REPOS="${CODEGRAPH_SOURCE_ONLY_REPOS:-hlt-web-service,hlt-gpt-researcher}"

is_skipped() {
  local slug="$1"
  [[ -z "$SKIP_REPOS" ]] && return 1
  local entry
  IFS=',' read -ra _skips <<< "$SKIP_REPOS"
  for entry in "${_skips[@]}"; do
    [[ "$(echo "$entry" | xargs)" == "$slug" ]] && return 0
  done
  return 1
}

is_source_only() {
  local slug="$1"
  local entry
  IFS=',' read -ra _source_only <<< "$SOURCE_ONLY_REPOS"
  for entry in "${_source_only[@]}"; do
    [[ "$(echo "$entry" | xargs)" == "$slug" ]] && return 0
  done
  return 1
}

freshness_stamp() {
  local slug="$1"
  if is_source_only "$slug"; then
    echo "$REPOS_DIR/$slug/.hlt-source-ready-at"
  else
    echo "$REPOS_DIR/$slug/.hlt-indexed-at"
  fi
}

# Skip repos indexed more recently than this. A container restart must not mean
# a fresh full index of everything: that is what made a single OOM self-
# sustaining. 0 disables the guard.
FRESH_HOURS="${CODEGRAPH_INDEX_FRESH_HOURS:-${CODEGRAPH_REINDEX_HOURS:-24}}"
FORCE="${CODEGRAPH_FORCE_REINDEX:-0}"

# Each daily `fetch --depth=1` adds a pack and nothing ever removed the old
# ones, so the 10 GB disk filled (2026-10-13: "No space left on device" on
# shallow.lock) while /health kept saying ok. Reclaim history every run and
# retain the last usable checkout if gc cannot recover enough room.
MIN_FREE_MB="${CODEGRAPH_MIN_FREE_MB:-1024}"
[[ "$MIN_FREE_MB" =~ ^[0-9]+$ ]] || MIN_FREE_MB=1024
# Read by server.py's public /health: "running|ok|failed <time> [reason]".
STATUS_FILE="$REPOS_DIR/.hlt-reindex-status"

mkdir -p "$REPOS_DIR"

# Boot, scheduled and manual invocations share one lock. Python locks the open
# file description inherited from Bash; descriptor 9 keeps it held for this run
# (and its Git children), even after the short lock helper exits.
exec 9>"$REPOS_DIR/.hlt-reindex.lock"
if python3 -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)
'; then
  :
else
  lock_status=$?
  if [[ "$lock_status" == "75" ]]; then
    echo "[codegraph] another reindex is running; leaving its status unchanged"
    exit 0
  fi
  echo "[codegraph] cannot acquire the reindex lock" >&2
  exit "$lock_status"
fi

# Empty when df can't say; an unknown reading never triggers a reclaim.
free_mb() {
  df -Pm "$REPOS_DIR" 2>/dev/null | awk 'NR==2 {print $4}' || true
}

disk_low() {
  local mb
  mb="$(free_mb)"
  [[ "$mb" =~ ^[0-9]+$ ]] && (( mb < MIN_FREE_MB ))
}

# Status writes must never abort the run, even on a full disk.
write_status() {
  { echo "$1 $(date -u +"%Y-%m-%dT%H:%M:%SZ")${2:+ $2}" > "$STATUS_FILE"; } 2>/dev/null || true
}

reclaim_git_space() {
  local dest="$1"
  [[ -d "$dest/.git" ]] || return 0
  # Never remove Git's locks: a separate operator may still own that operation.
  # `fetch origin HEAD` never moves the tracking refs, which would pin the
  # first clone's snapshot forever; nothing here reads them.
  git -C "$dest" for-each-ref --format='delete %(refname)' refs/remotes \
    | git -C "$dest" update-ref --no-deref --stdin >/dev/null 2>&1 || true
  # The refresh lock cannot coordinate an operator's independent Git writer.
  # Keep normal grace periods so its newly written, not-yet-referenced objects
  # survive cleanup. If older garbage cannot free enough room, retain the data.
  git -C "$dest" -c gc.reflogExpire=90.days.ago \
    -c gc.reflogExpireUnreachable=30.days.ago \
    gc --prune=2.weeks.ago --quiet >/dev/null 2>&1 || true
}

# A run that dies part way stays "running"; /health treats an old one as failed.
write_status running

echo "[codegraph] free space before reindex: $(free_mb) MB"

is_fresh() {
  local slug="$1"
  local stamp
  stamp="$(freshness_stamp "$slug")"
  [[ -f "$REPOS_DIR/$slug/.hlt-index-error" ]] && return 1
  [[ "$FORCE" == "1" ]] && return 1
  [[ "$FRESH_HOURS" == "0" ]] && return 1
  [[ -f "$stamp" ]] || return 1
  # ENOSPC can truncate a stamp and prevent the error marker from being written.
  # Its recent mtime alone must never turn that failed refresh into a fresh one.
  local recorded
  recorded="$(cat "$stamp" 2>/dev/null)" || return 1
  [[ "$recorded" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 1
  local age_s
  age_s=$(( $(date -u +%s) - $(date -u -r "$stamp" +%s 2>/dev/null || echo 0) ))
  (( age_s < FRESH_HOURS * 3600 ))
}

clone_or_update() {
  local slug="$1"
  local full="$2"
  local dest="$REPOS_DIR/$slug"
  local url
  if [[ -n "$TOKEN" ]]; then
    url="https://x-access-token:${TOKEN}@github.com/${full}.git"
  else
    url="https://github.com/${full}.git"
  fi

  if [[ -d "$dest/.git" ]]; then
    echo "[codegraph] updating $slug"
    reclaim_git_space "$dest"
    if disk_low; then
      echo "[codegraph] still low on disk after git cleanup; retaining $slug's checkout and index" >&2
      return 1
    fi
    # set -e does not apply inside a function called from `if`, so each step
    # fails the repo explicitly (on 2026-10-14 a failed fetch still logged
    # "checkout ready").
    git -C "$dest" fetch --depth=1 origin HEAD || return 1
    git -C "$dest" reset --hard FETCH_HEAD || return 1
    reclaim_git_space "$dest"
  else
    if disk_low; then
      echo "[codegraph] only $(free_mb) MB free (< $MIN_FREE_MB MB); not cloning $slug" >&2
      return 1
    fi
    echo "[codegraph] cloning $full -> $dest"
    if [[ -e "$dest" ]]; then
      echo "[codegraph] $slug has an incomplete checkout; retaining it for recovery" >&2
      return 1
    fi
    git clone --depth=1 "$url" "$dest" || return 1
  fi

  if is_source_only "$slug"; then
    echo "[codegraph] source checkout ready for $slug (structural indexing disabled)"
  else
    if disk_low; then
      echo "[codegraph] low disk after fetch; retaining $slug's existing index" >&2
      return 1
    fi
    echo "[codegraph] analyzing $slug"
    (cd "$dest" && gitnexus analyze --skip-agents-md --skip-skills) || return 1
  fi
}

FAILED_REPOS=()

# One repo failing must not abort the rest (set -e would otherwise stop the loop).
index_repo() {
  local slug="$1"
  local full="$2"
  if is_skipped "$slug"; then
    local stamp
    stamp="$(freshness_stamp "$slug")"
    local since="never indexed"
    [[ -f "$stamp" ]] && since="last indexed $(cat "$stamp")"
    echo "[codegraph] SKIPPING $slug — in CODEGRAPH_SKIP_REPOS ($since)." \
         "Its index is FROZEN and answers about it will go stale." >&2
    [[ ! -f "$REPOS_DIR/$slug/.hlt-index-error" ]] || FAILED_REPOS+=("$slug")
    return
  fi
  if is_fresh "$slug"; then
    echo "[codegraph] $slug refreshed $(cat "$(freshness_stamp "$slug")") — still fresh, skipping"
    return
  fi
  if clone_or_update "$slug" "$full" &&
     { date -u +"%Y-%m-%dT%H:%M:%SZ" > "$(freshness_stamp "$slug")"; } 2>/dev/null; then
    rm -f "$REPOS_DIR/$slug/.hlt-index-error"
    echo "[codegraph] indexed $slug"
  else
    { date -u +"%Y-%m-%dT%H:%M:%SZ indexing failed" > "$REPOS_DIR/$slug/.hlt-index-error"; } 2>/dev/null || true
    echo "[codegraph] FAILED to index $slug ($full); continuing" >&2
    FAILED_REPOS+=("$slug")
  fi
}

if [[ -n "$REPOS_SPEC" ]]; then
  IFS=',' read -ra ENTRIES <<< "$REPOS_SPEC"
  for entry in "${ENTRIES[@]}"; do
    entry="$(echo "$entry" | xargs)"
    [[ -z "$entry" ]] && continue
    if [[ "$entry" == *"|"* ]]; then
      slug="${entry%%|*}"
      full="${entry#*|}"
    else
      full="$entry"
      slug="$(basename "$entry")"
    fi
    index_repo "$slug" "$full"
  done
else
  for entry in "${DEFAULT_REPOS[@]}"; do
    slug="${entry%%|*}"
    full="${entry#*|}"
    index_repo "$slug" "$full"
  done
fi

if [[ ${#FAILED_REPOS[@]} -gt 0 ]]; then
  echo "[codegraph] reindex finished with failures: ${FAILED_REPOS[*]}" >&2
  write_status failed "${#FAILED_REPOS[@]} of the checkouts did not refresh"
  exit 1
else
  write_status ok
fi
echo "[codegraph] free space after reindex: $(free_mb) MB"

echo "[codegraph] indexed repos:"
gitnexus list || true
