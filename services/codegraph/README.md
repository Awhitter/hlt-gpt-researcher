# HLT Codegraph MCP (GitNexus)

Keeps nine estate checkouts current (Mastery Research, katailyst2, the Vault,
nursing-mastery, MMM2, Evidence-Based-Business, Design-registry, hlt-map, Mira)
and serves their code over streamable HTTP to Mastery Research's code scope and
to Katailyst agents. Every repo refreshes its code daily; the six smaller/medium
repos build structural GitNexus graphs sequentially with a per-repo heap cap,
timeout, crash-loop guard, and automatic source-only fallback, while the three
large monoliths (`katailyst2`, `nursing-mastery`, `scrapervault`) default to
source-only on the 2 GB Render plan so native tree-sitter/SQLite memory cannot
cgroup-OOM the container. Older graphs stay on disk and still answer the
structural tools; `/readiness` shows their date as `structuralIndexedAt` (and
any fallback reason as `graphError`). Upgrade `hlt-codegraph` to Render Pro
(4 GB) or Pro Plus (8 GB), clear `CODEGRAPH_SOURCE_ONLY_REPOS=""`, and bump
`CODEGRAPH_GRAPH_MAX_OLD_SPACE_MB` (e.g. `2048`) — or sync prebuilt `.gitnexus`
tarballs via `CODEGRAPH_PREBUILT_GRAPH_URL_TEMPLATE` — to build fresh graphs
for the three monoliths too.
`HLT-Master/hlt-web-service` left the default list on 8 Nov 2026 because the
GitHub account can no longer read it; add it back through `CODEGRAPH_REPOS` with
a token that can.

The image uses Node 24 LTS (pinned by digest), npm 12.0.2, and GitNexus 1.6.11.
The Python facade uses stable Python 3.14 on a pinned Bookworm image; Node and
its native modules come from the same Debian release. Cleo separately keeps
Python 3.13 because its Hermes runtime declares a lower maximum.
GitNexus is pinned to a reviewed stable release so a routine rebuild cannot
silently change the graph format or CLI. Existing source-only repositories,
boot-index choice, and refresh interval remain controlled by the same settings.

The Python MCP dependency intentionally remains below version 2. Current MCP
1.30 supports Python 3.14 and retains `mcp.server.fastmcp`, which this facade
uses; MCP 2.2 removed that module. The other Python dependency floors allow
current stable compatible releases. The image runs `assert_runtime.py` to
check installed versions, dependency consistency, native Node imports, actual
public-health output, and authentication decisions. This gate does not start
the server, clone repositories, create indexes, or load embedding models.

## Tools

- Exact source evidence: `search_source`, `read_source`, `verify_source_ref`
- Structural analysis: `list_repos`, `query`, `context`, `impact`, `trace`,
  `repo_overview`

Implementation questions should start with `search_source`, follow promising
matches with `read_source`, and cite the returned immutable URL. The structural
tools explain relationships but are not a substitute for reading the current
file before making a behavior claim.

## Endpoints

| Path | Auth | Returns |
|------|------|---------|
| `/health` | public | HTTP 200 liveness with a generic index warning: `status` is `degraded` when disk space is low, refresh is running/failed/stalled, or its status is missing/stale/unreadable; when GitHub refused a key it names the setting (`KATAILYST2_MCP_TOKEN`, `GITHUB_TOKEN`) and the cause, never a value: no Katailyst key is set, the key lacks the `github.token` scope, Katailyst refused the key, Katailyst did not answer, or Katailyst refused a repository (not covered by its GitHub App), given as a count; no repository identities |
| `/readiness` | bearer | per-repo index status, branch, commit SHA, `gitnexus_home` |
| `/verify-source` | bearer | validates one repo/path/full-commit tuple in the private checkout |
| `/mcp` | bearer | the tool surface |

`/health` is deliberately bare. Render's ipAllowList is `0.0.0.0/0`, so
anything it returns is world-readable, and index truth names nine private
repositories and their commit SHAs. If `CODEGRAPH_MCP_TOKEN` is unset the
service **fails closed**: every path except `/health` returns 503.
`tests/test_codegraph_auth.py` pins both.

## Env

| Var | Purpose |
|-----|---------|
| `KATAILYST2_MCP_TOKEN` | Katailyst key (`kata_...`) whose grant includes the `github.token` scope, for an HLT member or agent. Each run, `mint_github_token.py` asks Katailyst's GitHub App (`github.token.mint`) for a one-hour token with Contents: read on every configured Awhitter repository: one request for all of them (at most eight names a request), and one request per repository when Katailyst refuses that, because it refuses a whole request for one repository its App does not cover. Only that repository then falls back. Unset ⇒ the log says "no Katailyst credential set: using GITHUB_TOKEN". `reindex.sh` drops the variable from its environment at once, so only the mint helper is started with it, never `git` or `gitnexus` |
| `KATAILYST2_MCP_URL` | Katailyst MCP address (default `https://katailyst.com/mcp`). Must be `https`; plain `http` is accepted only for `localhost`, `127.0.0.1` and `[::1]`. A redirect is refused, so the key goes to that one origin only |
| `CODEGRAPH_MINT_TIMEOUT_S` | Wall-clock seconds one mint may take, the first one with the handshake before it (default 45). Katailyst's 15-second keepalives do not extend it. Past it the mint stops and the run falls back |
| `GITHUB_TOKEN` | Fallback read-only key (fine-grained, Contents: read), used when the mint fails, for a repository Katailyst's App does not cover, or for one outside the App (another owner). Neither key is printed, written into a checkout's `.git/config` or put in a URL: Git gets it from the environment of each command, and older clones that had a key in origin's URL are cleaned on their next refresh |
| `CODEGRAPH_MCP_TOKEN` | Bearer auth for `/mcp` and `/readiness`; unset ⇒ 503 |
| `CODEGRAPH_REPOS` | Optional `slug\|org/repo,...` override |
| `CODEGRAPH_REINDEX_HOURS` | Background reindex interval (default 24; `0` off) |
| `CODEGRAPH_SOURCE_ONLY_REPOS` | CSV checkouts refreshed for source tools without running local `gitnexus analyze` (default: `katailyst2,nursing-mastery,scrapervault`, the three 8k–15k-file monoliths on the 2 GB Render plan). Set explicitly to empty string `""` on a 4–8 GB Render plan to enable structural builds for all nine default repos |
| `CODEGRAPH_GRAPH_MAX_OLD_SPACE_MB` | Per-repo Node V8 `--max-old-space-size` cap applied to each sequential `gitnexus analyze` subprocess (default `512` for 2 GB Render; bump to `1536`–`3072` on a 4–8 GB Render plan). Paired with single-worker parser limits (`GITNEXUS_WORKER_POOL_SIZE=1`, `GITNEXUS_PARSE_CHUNK_CONCURRENCY=1`) |
| `CODEGRAPH_GRAPH_TIMEOUT_S` | Per-repo wall-clock timeout in seconds for `gitnexus analyze` (default `300`; `0` disables). On expiry, the entire process group is terminated (`SIGTERM` -> `SIGKILL`) so worker children never leak memory |
| `CODEGRAPH_GRAPH_FALLBACK_SOURCE_ONLY` | `1` (default) falls back cleanly to source-only if a single repo's `gitnexus analyze` fails, times out, or leaves a recent `.hlt-graph-in-progress` crash marker: preserves any existing `.gitnexus`, records `graphError` on `/readiness`, continues the remaining repos, and keeps public `/health` `ok`. Set `0` to treat graph build failures as hard refresh failures |
| `CODEGRAPH_PREBUILT_GRAPH_URL_TEMPLATE` | Optional `https://.../{slug}.tar.gz` URL template for syncing prebuilt `.gitnexus` graph archives (e.g. from a weekly Ubicloud `workflow_dispatch` build) into source-only repos without running `gitnexus analyze` inside the 2 GB Render container. `sync_prebuilt_graph.py` refuses non-HTTPS (except localhost), redirects, symlinks, and path traversal, and atomically swaps `.gitnexus` while preserving the existing graph on failure. Optional bearer header via `CODEGRAPH_PREBUILT_GRAPH_TOKEN` (stripped from other child environments) and size cap via `CODEGRAPH_PREBUILT_GRAPH_MAX_MB` (default `512`) |
| `SENTRY_DSN` | Optional HTTPS `sentry.io` DSN. When set, `reindex.sh` emits lightweight zero-dependency Sentry Cron Monitor check-in envelopes (`in_progress` -> `ok`/`error`) on monitor `hlt-codegraph-reindex` (24h interval, 120m margin, 60m max runtime) via `sentry_checkin.py`. Best-effort: never fails a refresh, refuses redirects, strips `SENTRY_DSN` from child environments, and never logs the DSN or key. Optional overrides: `CODEGRAPH_SENTRY_MONITOR_SLUG`, `SENTRY_ENVIRONMENT` |
| `CODEGRAPH_SKIP_REPOS` | CSV repos left completely untouched (code and graph frozen). Empty by default; use only to protect a checkout during recovery |
| `CODEGRAPH_SKIP_INDEX_ON_BOOT` | `1` to skip boot reindex |
| `CODEGRAPH_MIN_FREE_MB` | Free-space reserve (default 1024). Refresh prunes unused cached Git objects; if the reserve is still unmet, it preserves the checkout/index and reports failure. Unknown disk readings never trigger destructive recovery. `/health` uses the same threshold |
| `PORT` | Bind port (Render sets this) |

## Recovery and verification

Boot, scheduled and manual reindex runs share a nonblocking file lock. A second
run leaves the first run and its status alone. Frozen repositories are untouched;
an existing failure marker still counts as failed. A recent freshness stamp cannot
hide a failed refresh. Partial failure returns a nonzero exit code after trying
the other configured repositories.

Cleanup never deletes a checkout, `.git`, `.gitnexus`, or Git lock files. It expires
old reflogs, removes unused remote-tracking refs and prunes cached objects only for
configured, non-frozen checkouts being refreshed. Git cleanup retains the normal
90/30-day reflog and two-week object grace periods to protect independent writers;
it never uses immediate pruning. A retry remains unverified until it finishes;
starting it cannot erase a degraded health signal. If a full disk prevents Git GC,
or a checkout is incomplete, the run stops refreshing that repository and keeps
the last available data for an operator. Do not remove an index to make space:
rebuilding it can exceed this service's memory limit.

After an authorized release, verify the serving commit in Render, disk headroom,
and a completed scheduled refresh with no errors. Public `/health` must remain
HTTP 200 without exposing repository names; its `ok` status is not sufficient
proof. Authenticated `/readiness` must show the expected repository SHAs and fresh
timestamps, and a source read must resolve an immutable link at that SHA. Frozen
repositories remain explicitly stale.

`python3 mint_github_token.py --dry-run katailyst2` prints the token request it
would send and whether a Katailyst credential is set, without calling anything.

Each run logs one line per repository saying which key its refresh used and why,
for example `the-vault: GitHub key is GITHUB_TOKEN, the fallback (Katailyst refused
this repository: not covered by the GitHub App)`. The same record is on the status
line in `$REPOS_DIR/.hlt-reindex-status` as `repos=slug:source:why:result,...`,
where the source is `katailyst`, `github-token` or `none`, and the reason is
`minted`, `not-covered`, `scope`, `unset`, `key-refused`, `no-answer`, `refused` or
`outside-app`. Public `/health` reads that line and reports causes and counts only.

The regression suite uses disposable local Git repositories, a stand-in Katailyst
and a git shim that records which key each fetch carried, plus a stub graph CLI:
`python -m pytest tests/test_codegraph_auth.py`. It does not rebuild production
indexes or prove that the production disk has recovered.

## Local

```bash
docker build -t hlt-codegraph .
docker run --rm -p 8080:8080 \
  -e KATAILYST2_MCP_TOKEN \
  -e GITHUB_TOKEN \
  -e CODEGRAPH_MCP_TOKEN=dev \
  -v codegraph-data:/data \
  hlt-codegraph
```

Point GPT Researcher at it:

```
CODEGRAPH_MCP_URL=https://<service>/mcp
CODEGRAPH_MCP_TOKEN=dev
```
