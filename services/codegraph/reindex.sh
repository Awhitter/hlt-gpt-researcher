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

mkdir -p "$REPOS_DIR"

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
    git -C "$dest" fetch --depth=1 origin HEAD
    git -C "$dest" reset --hard FETCH_HEAD
  else
    echo "[codegraph] cloning $full -> $dest"
    rm -rf "$dest"
    git clone --depth=1 "$url" "$dest"
  fi

  if is_source_only "$slug"; then
    echo "[codegraph] source checkout ready for $slug (structural indexing disabled)"
  else
    echo "[codegraph] analyzing $slug"
    (cd "$dest" && gitnexus analyze --skip-agents-md --skip-skills)
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
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$(freshness_stamp "$slug")"
    rm -f "$REPOS_DIR/$slug/.hlt-index-error"
    echo "[codegraph] indexed $slug"
  else
    date -u +"%Y-%m-%dT%H:%M:%SZ indexing failed" > "$REPOS_DIR/$slug/.hlt-index-error" 2>/dev/null || true
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
fi

echo "[codegraph] indexed repos:"
gitnexus list || true
