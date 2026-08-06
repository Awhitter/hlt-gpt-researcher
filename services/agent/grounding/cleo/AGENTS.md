# Nursing Mastery — what you need to explain it

www.nursingmastery.com. Alec built essentially all of it solo with AI, fast:
~265,000 lines, 1,401 files, 77 page routes, 87 API handlers, 309 test files, and
**985 commits in the last 30 days**. That velocity is why documentation lags
reality in places, and why the code is the final authority.

## The single most load-bearing fact

**Nursing Mastery has no database.** None. It owns the nurse-facing relationship
and presentation; **ScraperVault** owns jobs, employers, People, interest,
applications, forum, content and canonical funnel truth; PostHog owns browser
behaviour. Browser storage is explicitly "mirrors, not truth".

Anyone reasoning about data in this repo without knowing that will be wrong. Say
it early when a data question comes up.

## What a visitor can do

Jobs (10,000+ live, board + map + detail + apply) · one universal application ·
**18 career tools** under `/tools` (Pulse, resume builder, interview practice,
pay and offer analysis, licensure lookup, specialty comparison, residency
finder…) · editorial (articles, series, topics, news) · community forums and
webinars · The Shift Report newsletter · employer surfaces · **Mira**, the nurse
agent · admin.

## Shape of the code

Five route groups under `app/`, which is the first thing that confuses people:
`(home)` is `/`; `(site)` is the main shell and holds ~70 routes; `(app)` is the
Locker canvas; `(bare)` is mostly legacy redirects; `(runway)` is a stripped
noindex shell for cold NCLEX traffic.

`proxy.ts` is the request seam (Next 16's rename of middleware) — test-prep
arrival claims, ad click ids, the paid-hero experiment.

Stack: Next 16 / React 19 / Node 24 / TypeScript / Tailwind. Vercel primary.

External services that matter: **ScraperVault Feed v1** (source of truth),
PostHog, Vercel AI Gateway (the resume agent), **katailyst-eve** (Mira's actual
runtime), **katailyst2 MCP** (the Locker board), Mapbox, ElevenLabs, Resend,
Supabase (staff/employer sessions), Cloudinary.

`lib/scrapervault-client.ts` is 3,341 lines **vendored byte-identical** from
ScraperVault and gate-checked in both repos. Changing it is a two-PR ritual.

## Where a new engineer will get lost

Warn them before they wander in:

1. `lib/apply/resume-agent.ts` — 3,974 lines, a multi-stage AI pipeline whose
   correctness is *evaluated*, not asserted. Nobody reasons about this by reading.
2. **Four overlapping identity systems**: signed device cookies, Supabase human
   sessions for staff and employers, signed-link redemption, and a silent
   test-prep claim that "buys SAVES, not SESSIONS". The signed link is the one
   deliberate CSRF exemption in the whole app — Gmail sends cross-site, so the
   token *is* the authentication. Nothing else may copy it.
3. **Capture merge semantics** — replacing an answer needs a matching checksum
   (compare-and-set). Before that fix, a nurse answering twice silently lost the
   first answer.
4. **Three ranking modules that must agree** — with no attributes the ordering
   must be the identity function, or the server pass and the client re-rank
   disagree and you get hydration drift no test names.
5. `lib/jobs/jobs-pagination.mjs` — filter options hardcoded from live data
   distribution, with counts in comments. Will rot as the data shifts.
6. **Mira spans three repos** — renders here, runs in katailyst-eve, board on
   katailyst2's canvas engine.
7. **The ratchets** — `card-chrome`, `button-chrome`, `copy-voice` baselines are
   shrink-only budgets, not pass/fail. A PR failing on a number confuses everyone
   the first time.

## The documentation, and its altitude

Fourteen maintained docs, all kept fresh mechanically by `npm run docs:check` in
CI. `docs/SYSTEM.md` is the closest thing to an architecture overview.
`docs/ATLAS.md` is generated and never hand-edited. Also OPERATIONS, PRODUCT,
ANALYTICS, VOICE, PAGE_SPINE, JOURNEY, FUNNEL_WALK.

They are accurate and they are written agent-to-agent. **ScraperVault has a
plain-language `docs/TEAM-GUIDE.md`; this repo has no equivalent.** You are the
substitute. Read the contract, hand back the meaning.

## Where authority actually lives

1. **ScraperVault `docs/DECISIONS.md`** — owner rulings, D-numbered. Outranks
   anything in the nursing-mastery repo on product and funnel facts.
2. **Linear `NUR`** — the roadmap ledger.
3. The repo docs — how it works today.
4. The code — final, when docs and code disagree.

## The Linear board

One team, **`NUR`**, holds **both** Nursing Mastery and ScraperVault work.
`repo:*` labels are the only thing separating them — a "Nursing Mastery" answer
that ignores labels is quietly mixing in ScraperVault work.

Roughly 250+ open, ~15 projects (Nursing Mastery Growth, Wave 1 Discovery wow,
Wave 2 Personal layer, Wave 3 Places & tools, Platform Feed & data, Content &
Shift Report, Hospitals & revenue, ScraperVault Intelligence, Owner inbox…).

State of the board, which is honest context when someone asks "what's the plan":
most issues have no project, most have no priority, there are no estimates
anywhere, and almost everything is unassigned because until now there was one
person. No sprints or cycles exist — "this sprint" has no meaning here.

## Answering "what shipped"

Use the repos, not Linear. Linear's completed feed is capped and ordered by last
touched, while nursing-mastery merges hundreds of PRs a fortnight. Both repos
keep a thematic, plain-language `CHANGELOG.md`, and a coverage gate proves every
merged PR since 2026-07-06 appears in it. That is the real record, already
written the way a new hire needs it.

Use Linear for what is **open** — in flight, upcoming, and the status of a
specific thing.

## Your tools for this

**Linear (what is open).** `linear_in_flight`, `linear_upcoming`,
`linear_board_health`, `linear_issue`. Always pass
`repo_label="repo:nursing-mastery"` when the question is about Nursing Mastery —
without it you will report ScraperVault work as ours. On the live board that is
the difference between 25 in-flight issues and 7.

**Writing to Linear.** `linear_file_issue` and `linear_update_issue`, one issue
at a time. Confirm in the thread before either. `what`, `why` and `done_when` are
required because an issue without them cannot be picked up cold — and this board
already has dozens like that. `linear_update_issue` returns before and after;
paste both.

**What shipped.** `recent_changes(repo, days)` on the code graph. It reads the
repo's CHANGELOG, which is dated, thematic, plain-language prose kept complete by
a coverage gate. Use it rather than Linear for finished work: these repos merge
hundreds of PRs a fortnight and Linear's completed feed is capped.

**How things work.** `query`, `context`, `impact`, `trace` on the code graph, and
`deep_research` for anything needing the web or several systems at once.

Rule of thumb: **Linear for open, CHANGELOG for done, code graph for how.**
