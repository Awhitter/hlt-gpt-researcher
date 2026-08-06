#!/usr/bin/env bash
set -euo pipefail

REPOS_DIR="${REPOS_DIR:-/data/repos}"
GITNEXUS_HOME="${GITNEXUS_HOME:-/data/gitnexus}"
export GITNEXUS_HOME

# slug|github_org/repo
DEFAULT_REPOS=(
  "mmm2|Awhitter/MMM2"
  "katailyst2|Awhitter/katailyst2"
  "ebb|Awhitter/evidence-based-business"
  "scrapervault|Awhitter/ScraperVault"
  "nursing-mastery|Awhitter/nursing-mastery"
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
# boot reindex used to immediately re-run the work that caused the OOM. The real
# fixes are a larger plan or moving indexing out of the web service; until then a
# repo too big to index stays stale instead of taking the service down with it.

# Skip repos indexed more recently than this. A container restart must not mean
# a fresh full index of everything: that is what made a single OOM self-
# sustaining. 0 disables the guard.
FRESH_HOURS="${CODEGRAPH_INDEX_FRESH_HOURS:-${CODEGRAPH_REINDEX_HOURS:-24}}"
FORCE="${CODEGRAPH_FORCE_REINDEX:-0}"

mkdir -p "$REPOS_DIR"

is_fresh() {
  local slug="$1"
  local stamp="$REPOS_DIR/$slug/.hlt-indexed-at"
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

  echo "[codegraph] analyzing $slug"
  (cd "$dest" && gitnexus analyze --skip-agents-md --skip-skills)
}

FAILED_REPOS=()

# One repo failing must not abort the rest (set -e would otherwise stop the loop).
index_repo() {
  local slug="$1"
  local full="$2"
  if is_fresh "$slug"; then
    echo "[codegraph] $slug indexed $(cat "$REPOS_DIR/$slug/.hlt-indexed-at") — still fresh, skipping"
    return
  fi
  if clone_or_update "$slug" "$full"; then
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$REPOS_DIR/$slug/.hlt-indexed-at"
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
