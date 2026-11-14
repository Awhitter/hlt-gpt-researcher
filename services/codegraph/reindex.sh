#!/usr/bin/env bash
set -euo pipefail
# This script holds GitHub credentials in memory; a trace would print them.
{ set +x; } 2>/dev/null

REPOS_DIR="${REPOS_DIR:-/data/repos}"
GITNEXUS_HOME="${GITNEXUS_HOME:-/data/gitnexus}"
export GITNEXUS_HOME

# slug|github_org/repo
DEFAULT_REPOS=(
  "hlt-gpt-researcher|Awhitter/hlt-gpt-researcher"
  "mmm2|Awhitter/MMM2"
  "katailyst2|Awhitter/katailyst2"
  "ebb|Awhitter/evidence-based-business"
  # ScraperVault was renamed the-vault; GitHub redirects the old name, but Katailyst's
  # GitHub App knows only the current one.
  "scrapervault|Awhitter/the-vault"
  "nursing-mastery|Awhitter/nursing-mastery"
)
# HLT-Master/hlt-web-service left the list on 8 Nov 2026: the Awhitter account can no
# longer see it ("Repository not found"), so every run failed and /health stayed
# degraded. Add it back through CODEGRAPH_REPOS with a token that can read HLT-Master.

# GitHub credentials. Since the 8 Nov 2026 key rotation GitHub refused GITHUB_TOKEN
# and every refresh failed. Each run now asks Katailyst's GitHub App for a one-hour,
# read-only token (github.token.mint, through mint_github_token.py) for the Awhitter
# repositories it refreshes; GITHUB_TOKEN is only the fallback, for a failed mint or
# a repository the App does not cover. Git gets the key from the environment of each
# git command, never from the remote URL, so no checkout on the disk keeps a copy.
# Until 8 Nov 2026 the key was baked into each clone's origin URL.
GITHUB_PAT="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
export GIT_TERMINAL_PROMPT=0
# The Katailyst key can mint tokens, so no child of this run may inherit it. It is
# read once into a shell variable that is never exported (unset first: a variable
# that arrived through the environment stays exported), and both environment names
# are dropped before anything else starts. Only the mint helper gets it, in that
# one command's environment.
unset KATAILYST_KEY
KATAILYST_KEY="${KATAILYST2_MCP_TOKEN:-${KATAILYST_MCP_TOKEN:-}}"
unset KATAILYST2_MCP_TOKEN KATAILYST_MCP_TOKEN
MINT_HELPER="${CODEGRAPH_MINT_HELPER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mint_github_token.py}"
MINT_STATE=""      # ok | partial | unset | failed | unused, once the first refresh needs GitHub
# What the mint helper said for each repository it was asked about, index by index.
MINT_NAMES=()      # "awhitter/katailyst2", lower case
MINT_TOKENS=()     # the token Katailyst minted for it, or ""
MINT_WHYS=()       # "minted", or why it has no token (the reasons listed at AUTH_WHY)
MINT_DEFAULT_WHY="refused" # for a repository the helper said nothing about
AUTH_TOKEN=""      # the key for the repository being refreshed
AUTH_SOURCE=""     # katailyst | github-token | none
# Why that key: minted, or why not (not-covered | scope | unset | key-refused |
# no-answer | refused | outside-app). server.py's /health turns each into words.
AUTH_WHY=""
AUTH_REPO_REFUSED="" # 1 once GitHub refused the key for the repository being refreshed
AUTH_REFUSED=""    # credential sources GitHub refused this run, comma-separated
AUTH_RECORDS=""    # "slug:source:why:ok|refused|failed,..." for the status line
# What Git prints when GitHub turns a key down (8 Nov 2026: "could not read Username").
AUTH_ERROR_PATTERN='could not read Username|Authentication failed|Invalid username or password|returned error: 40[13]|terminal prompts disabled'
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
# Every default repo refreshes as source only. GitNexus rebuilds of the five large
# repos ran the 2 GB container out of memory, so they had sat on CODEGRAPH_SKIP_REPOS
# with their code frozen at Aug-Sep copies. Source tools need current code more than
# a graph; remove a slug from this list to rebuild its graph again.
SOURCE_ONLY_REPOS="${CODEGRAPH_SOURCE_ONLY_REPOS:-hlt-gpt-researcher,katailyst2,nursing-mastery,scrapervault,mmm2,ebb}"

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
# Read by server.py's public /health: "running|ok|failed <time> [reason]" or
# "paused <time> <count> <oldest last-indexed time|never>".
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

# Clean the saved origin before selection or freshness can skip a checkout.
# Read the raw local setting, without applying includes or URL rewrite rules.
scrub_remote_credentials() {
  local config="$1" urls
  [[ -f "$config" && -r "$config" && ! -L "$config" ]] || return 1
  if urls="$(git config --file "$config" --get-all remote.origin.url 2>/dev/null)"; then
    if [[ "$urls" =~ https://[^/@[:space:]]+@github\.com/ ]]; then
      # Legacy clones have exactly one origin. Never guess which entry to replace.
      [[ "$urls" =~ ^https://[^/@[:space:]]+@github\.com/([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)$ ]] || return 1
      git config --file "$config" remote.origin.url "https://github.com/${BASH_REMATCH[1]}" 2>/dev/null || return 1
    fi
  else
    # An absent origin is fine. A malformed configuration must be investigated.
    [[ $? == 1 ]] || return 1
  fi
}

write_status running
checked_origins=0
for checkout in "$REPOS_DIR"/*; do
  [[ -d "$checkout/.git" && ! -L "$checkout" && ! -L "$checkout/.git" ]] || continue
  if ! scrub_remote_credentials "$checkout/.git/config"; then
    write_status failed "could not clean a saved Git origin"
    echo "[codegraph] could not clean a saved Git origin; refresh stopped" >&2
    exit 1
  fi
  checked_origins=$((checked_origins + 1))
done
echo "[codegraph] checked saved Git origins in $checked_origins retained checkouts"

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

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# The configured repositories, read once: the token request needs all of them.
REPO_SLUGS=()
REPO_FULLS=()
if [[ -n "$REPOS_SPEC" ]]; then
  IFS=',' read -ra ENTRIES <<< "$REPOS_SPEC"
  for entry in "${ENTRIES[@]}"; do
    entry="$(echo "$entry" | xargs)"
    [[ -z "$entry" ]] && continue
    if [[ "$entry" == *"|"* ]]; then
      REPO_SLUGS+=("${entry%%|*}")
      REPO_FULLS+=("${entry#*|}")
    else
      REPO_SLUGS+=("$(basename "$entry")")
      REPO_FULLS+=("$entry")
    fi
  done
else
  for entry in "${DEFAULT_REPOS[@]}"; do
    REPO_SLUGS+=("${entry%%|*}")
    REPO_FULLS+=("${entry#*|}")
  done
fi

# Ask Katailyst once per run, the first time a refresh needs GitHub, for tokens
# covering every configured Awhitter repository. The helper asks for all of them
# in one request and, when Katailyst refuses that, once per repository, so one
# uncovered repository cannot turn the minted token off for the rest. It answers
# one line per repository: "minted<TAB>name<TAB>token" or
# "fallback<TAB>name<TAB>why". Never prints either key.
prepare_github_auth() {
  [[ -n "$MINT_STATE" ]] && return 0
  local names=() i owner name out="" rc=0 kind value minted=0
  MINT_STATE=unused
  [[ ${#REPO_FULLS[@]} -gt 0 ]] || return 0
  for i in "${!REPO_FULLS[@]}"; do
    is_skipped "${REPO_SLUGS[$i]}" && continue
    owner="${REPO_FULLS[$i]%%/*}"
    name="${REPO_FULLS[$i]#*/}"
    name="${name%.git}"
    [[ "$(lower "$owner")" == "awhitter" ]] || continue
    # A name GitHub could not hold is not sent: it is not covered, whatever else is.
    if [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]]; then
      names+=("$name")
    else
      MINT_NAMES+=("awhitter/$(lower "$name")")
      MINT_TOKENS+=("")
      MINT_WHYS+=("not-covered")
    fi
  done
  local fallback="using GITHUB_TOKEN"
  [[ -n "$GITHUB_PAT" ]] || fallback="and GITHUB_TOKEN is not set either"
  [[ ${#names[@]} -gt 0 ]] || return 0
  out="$(KATAILYST2_MCP_TOKEN="$KATAILYST_KEY" python3 "$MINT_HELPER" "${names[@]}")" || rc=$?
  KATAILYST_KEY=""
  # Read through a pipe: a here-string would put the tokens in a temporary file.
  while IFS=$'\t' read -r kind name value; do
    [[ -n "$name" && -n "$value" ]] || continue
    case "$kind" in
      minted)
        MINT_NAMES+=("awhitter/$(lower "$name")")
        MINT_TOKENS+=("$value")
        MINT_WHYS+=("minted")
        minted=$((minted + 1))
        ;;
      fallback)
        [[ "$value" =~ ^[a-z-]+$ ]] || value=refused
        MINT_NAMES+=("awhitter/$(lower "$name")")
        MINT_TOKENS+=("")
        MINT_WHYS+=("$value")
        ;;
    esac
  done < <(printf '%s\n' "$out")
  out=""
  value=""
  case "$rc" in
    0) MINT_STATE=ok ;;
    2)
      MINT_STATE=partial
      echo "[codegraph] Katailyst minted a GitHub token for $minted of ${#names[@]} repositories; for the rest: $fallback" >&2
      ;;
    3)
      MINT_STATE="unset"
      MINT_DEFAULT_WHY="unset"
      echo "[codegraph] no Katailyst credential set: $fallback" >&2
      ;;
    *)
      MINT_STATE=failed
      echo "[codegraph] Katailyst did not mint a GitHub token: $fallback" >&2
      ;;
  esac
}

# Pick the key for one repository: Katailyst's token when it minted one for the
# repository, else GITHUB_TOKEN, else none (a public repository still clones).
# AUTH_WHY keeps the reason, for the log line and the status line.
select_github_auth() {
  local full i
  full="$(lower "${1%.git}")"
  prepare_github_auth
  AUTH_TOKEN=""
  AUTH_WHY="$MINT_DEFAULT_WHY"
  AUTH_REPO_REFUSED=""
  [[ "$full" == awhitter/* ]] || AUTH_WHY=outside-app
  for ((i = 0; i < ${#MINT_NAMES[@]}; i++)); do
    [[ "${MINT_NAMES[$i]}" == "$full" ]] || continue
    AUTH_TOKEN="${MINT_TOKENS[$i]}"
    AUTH_WHY="${MINT_WHYS[$i]}"
    break
  done
  if [[ -n "$AUTH_TOKEN" ]]; then
    AUTH_SOURCE=katailyst
  elif [[ -n "$GITHUB_PAT" ]]; then
    AUTH_TOKEN="$GITHUB_PAT"
    AUTH_SOURCE=github-token
  else
    AUTH_SOURCE=none
  fi
}

# One log line per repository: which key its refresh uses and why. Settings and
# repository names only, never a value.
say_github_auth() {
  local slug="$1" why
  case "$AUTH_WHY" in
    minted)
      echo "[codegraph] $slug: GitHub key is the token Katailyst minted"
      return 0
      ;;
    not-covered) why="Katailyst refused this repository: not covered by the GitHub App" ;;
    scope) why="the key in KATAILYST2_MCP_TOKEN lacks the github.token scope" ;;
    unset) why="no Katailyst credential is set" ;;
    key-refused) why="Katailyst refused the key in KATAILYST2_MCP_TOKEN" ;;
    no-answer) why="Katailyst did not answer the token request" ;;
    outside-app) why="its owner is outside Katailyst's GitHub App" ;;
    *) why="Katailyst did not mint a token for it" ;;
  esac
  if [[ "$AUTH_SOURCE" == github-token ]]; then
    echo "[codegraph] $slug: GitHub key is GITHUB_TOKEN, the fallback ($why)" >&2
  else
    echo "[codegraph] $slug: no GitHub key ($why, and GITHUB_TOKEN is not set)" >&2
  fi
}

# Keep one repository's key source, reason and result for the status line.
note_github_auth() {
  [[ -n "$AUTH_SOURCE" ]] || return 0
  AUTH_RECORDS="${AUTH_RECORDS:+$AUTH_RECORDS,}$1:$AUTH_SOURCE:$AUTH_WHY:$2"
}

# git with the selected key in that one command's environment only: never in a
# URL, a .git/config, a command line or the log.
git_auth() {
  if [[ -z "$AUTH_TOKEN" ]]; then
    git "$@"
    return
  fi
  (
    n="${GIT_CONFIG_COUNT:-0}"
    export "GIT_CONFIG_KEY_${n}=http.https://github.com/.extraheader"
    export "GIT_CONFIG_VALUE_${n}=AUTHORIZATION: basic $(printf 'x-access-token:%s' "$AUTH_TOKEN" | base64 | tr -d '\n')"
    export GIT_CONFIG_COUNT=$((n + 1))
    exec git "$@"
  )
}

# A network git step: as git_auth, and notes when GitHub refused the key.
git_net() {
  local errfile rc=0
  errfile="$(mktemp "${TMPDIR:-/tmp}/codegraph-git.XXXXXX" 2>/dev/null)" || { git_auth "$@"; return; }
  git_auth "$@" 2>"$errfile" || rc=$?
  cat "$errfile" >&2 || true
  if [[ "$rc" != 0 ]] && grep -qiE "$AUTH_ERROR_PATTERN" "$errfile" 2>/dev/null; then
    AUTH_REPO_REFUSED=1
    [[ ",$AUTH_REFUSED," == *",$AUTH_SOURCE,"* ]] || AUTH_REFUSED="${AUTH_REFUSED:+$AUTH_REFUSED,}$AUTH_SOURCE"
    case "$AUTH_SOURCE" in
      katailyst) echo "[codegraph] GitHub refused the token Katailyst minted" >&2 ;;
      github-token) echo "[codegraph] GitHub refused GITHUB_TOKEN" >&2 ;;
      *) echo "[codegraph] GitHub asked for a key and none is set (KATAILYST2_MCP_TOKEN, GITHUB_TOKEN)" >&2 ;;
    esac
  fi
  rm -f "$errfile"
  return "$rc"
}

clone_or_update() {
  local slug="$1"
  local full="$2"
  local dest="$REPOS_DIR/$slug"
  local url="https://github.com/${full}.git"
  select_github_auth "$full"
  say_github_auth "$slug"

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
    git_net -C "$dest" fetch --depth=1 origin HEAD || return 1
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
    git_net clone --depth=1 "$url" "$dest" || return 1
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
PAUSED_REPOS=()
# Oldest last-indexed time among paused repos, or "never".
PAUSED_SINCE=""

# One repo failing must not abort the rest (set -e would otherwise stop the loop).
index_repo() {
  local slug="$1"
  local full="$2"
  # The locked cleanup pass covers only real, immediate child checkouts.
  if [[ ! "$slug" =~ ^[[:alnum:]_][[:alnum:]_.-]*$ || -L "$REPOS_DIR/$slug" || -L "$REPOS_DIR/$slug/.git" ]]; then
    write_status failed "unsafe checkout path"
    echo "[codegraph] unsafe checkout path; refresh stopped" >&2
    exit 1
  fi
  if is_skipped "$slug"; then
    local stamp
    stamp="$(freshness_stamp "$slug")"
    local since="never indexed"
    [[ -f "$stamp" ]] && since="last indexed $(cat "$stamp")"
    echo "[codegraph] SKIPPING $slug — in CODEGRAPH_SKIP_REPOS ($since)." \
         "Its index is FROZEN and answers about it will go stale." >&2
    # A pause, not a failure: the run did everything it was allowed to do. Any
    # .hlt-index-error stays put, so /readiness still shows the last attempt.
    PAUSED_REPOS+=("$slug")
    local recorded=""
    [[ -f "$stamp" ]] && recorded="$(cat "$stamp" 2>/dev/null)" || true
    if [[ ! "$recorded" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
      PAUSED_SINCE="never"
    elif [[ "$PAUSED_SINCE" != "never" && ( -z "$PAUSED_SINCE" || "$recorded" < "$PAUSED_SINCE" ) ]]; then
      PAUSED_SINCE="$recorded"
    fi
    return
  fi
  if is_fresh "$slug"; then
    echo "[codegraph] $slug refreshed $(cat "$(freshness_stamp "$slug")") — still fresh, skipping"
    return
  fi
  AUTH_SOURCE=""
  AUTH_WHY=""
  AUTH_REPO_REFUSED=""
  if clone_or_update "$slug" "$full" &&
     { date -u +"%Y-%m-%dT%H:%M:%SZ" > "$(freshness_stamp "$slug")"; } 2>/dev/null; then
    note_github_auth "$slug" ok
    rm -f "$REPOS_DIR/$slug/.hlt-index-error"
    echo "[codegraph] indexed $slug"
  else
    if [[ -n "$AUTH_REPO_REFUSED" ]]; then
      note_github_auth "$slug" refused
    else
      note_github_auth "$slug" failed
    fi
    { date -u +"%Y-%m-%dT%H:%M:%SZ indexing failed" > "$REPOS_DIR/$slug/.hlt-index-error"; } 2>/dev/null || true
    echo "[codegraph] FAILED to index $slug ($full); continuing" >&2
    FAILED_REPOS+=("$slug")
  fi
}

if [[ ${#REPO_SLUGS[@]} -gt 0 ]]; then
  for i in "${!REPO_SLUGS[@]}"; do
    index_repo "${REPO_SLUGS[$i]}" "${REPO_FULLS[$i]}"
  done
fi
MINT_TOKENS=()
AUTH_TOKEN=""
KATAILYST_KEY=""

# Which key each refreshed repository used and why, for the status line. /health
# turns it into a plain reason: settings and counts, never a value or a name.
AUTH_DETAIL=""
if [[ -n "$AUTH_RECORDS" ]]; then
  AUTH_DETAIL="github-auth mint=${MINT_STATE:-unused}${AUTH_REFUSED:+ refused=$AUTH_REFUSED} repos=$AUTH_RECORDS"
fi

if [[ ${#FAILED_REPOS[@]} -gt 0 ]]; then
  echo "[codegraph] reindex finished with failures: ${FAILED_REPOS[*]}" >&2
  if [[ -n "$AUTH_REFUSED" ]]; then
    write_status failed "$AUTH_DETAIL"
  else
    write_status failed "${#FAILED_REPOS[@]} of the checkouts did not refresh"
  fi
  exit 1
elif [[ ${#PAUSED_REPOS[@]} -gt 0 ]]; then
  echo "[codegraph] reindex finished; paused: ${PAUSED_REPOS[*]}"
  write_status paused "${#PAUSED_REPOS[@]} $PAUSED_SINCE${AUTH_DETAIL:+ $AUTH_DETAIL}"
else
  write_status ok "$AUTH_DETAIL"
fi
echo "[codegraph] free space after reindex: $(free_mb) MB"

echo "[codegraph] indexed repos:"
gitnexus list || true
