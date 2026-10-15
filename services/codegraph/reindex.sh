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
# rebuild history in place when gc itself has no room.
MIN_FREE_MB="${CODEGRAPH_MIN_FREE_MB:-1024}"
[[ "$MIN_FREE_MB" =~ ^[0-9]+$ ]] || MIN_FREE_MB=1024
# Read by server.py's public /health: "running|ok|failed <time> [reason]".
STATUS_FILE="$REPOS_DIR/.hlt-reindex-status"

mkdir -p "$REPOS_DIR"

# Empty when df can't say; an unknown reading never triggers a reclaim.
free_mb() {
  df -Pm "$REPOS_DIR" 2>/dev/null | awk 'NR==2 {print $4}' || true
}

disk_low() {
  local mb
  mb="$(free_mb)"
  [[ -n "$mb" ]] && (( mb < MIN_FREE_MB ))
}

# Status writes must never abort the run, even on a full disk.
write_status() {
  { echo "$1 $(date -u +"%Y-%m-%dT%H:%M:%SZ")${2:+ $2}" > "$STATUS_FILE"; } 2>/dev/null || true
}

reclaim_git_space() {
  local dest="$1"
  [[ -d "$dest/.git" ]] || return 0
  # A run killed mid-fetch leaves these behind, and git refuses to start again.
  rm -f "$dest/.git/shallow.lock" "$dest/.git/index.lock"
  # `fetch origin HEAD` never moves the tracking refs, which would pin the
  # first clone's snapshot forever; nothing here reads them.
  git -C "$dest" for-each-ref --format='delete %(refname)' refs/remotes \
    | git -C "$dest" update-ref --no-deref --stdin >/dev/null 2>&1 || true
  git -C "$dest" reflog expire --expire=now --all >/dev/null 2>&1 || true
  git -C "$dest" gc --prune=now --quiet >/dev/null 2>&1 || true
}

# A run that dies part way stays "running"; /health treats an old one as failed.
write_status running

echo "[codegraph] free space before reindex: $(free_mb) MB"
if disk_low; then
  echo "[codegraph] low disk; reclaiming git history in every checkout" >&2
  for dest in "$REPOS_DIR"/*/; do
    reclaim_git_space "${dest%/}"
  done
  echo "[codegraph] free space after reclaim: $(free_mb) MB"
fi

is_fresh() {
  local slug="$1"
  local stamp
  stamp="$(freshness_stamp "$slug")"
  [[ "$FORCE" == "1" ]] && return 1
  [[ "$FRESH_HOURS" == "0" ]] && return 1
  [[ -f "$stamp" ]] || return 1
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
    rm -f "$dest/.git/shallow.lock" "$dest/.git/index.lock"
    if disk_low; then
      # gc needs room to write a new pack, so a full disk can't be reclaimed
      # that way. Rebuild only this checkout's history in place: the working
      # tree keeps its .gitnexus index (a full re-analyze can run out of
      # memory) and its freshness stamp.
      echo "[codegraph] still low on disk; rebuilding $slug's git history in place" >&2
      local origin_url
      origin_url="$(git -C "$dest" remote get-url origin 2>/dev/null || echo "$url")"
      rm -rf "$dest/.git"
      git -C "$dest" init -q || return 1
      git -C "$dest" remote add origin "$origin_url" || return 1
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
    rm -rf "$dest"
    git clone --depth=1 "$url" "$dest" || return 1
  fi

  if is_source_only "$slug"; then
    echo "[codegraph] source checkout ready for $slug (structural indexing disabled)"
  else
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
    return
  fi
  if is_fresh "$slug"; then
    echo "[codegraph] $slug refreshed $(cat "$(freshness_stamp "$slug")") — still fresh, skipping"
    return
  fi
  if clone_or_update "$slug" "$full"; then
    { date -u +"%Y-%m-%dT%H:%M:%SZ" > "$(freshness_stamp "$slug")"; } 2>/dev/null || true
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
else
  write_status ok
fi
echo "[codegraph] free space after reindex: $(free_mb) MB"

echo "[codegraph] indexed repos:"
gitnexus list || true
