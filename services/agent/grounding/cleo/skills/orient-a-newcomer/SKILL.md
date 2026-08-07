---
name: orient-a-newcomer
description: "Explain Nursing Mastery (or one of its subsystems) to somebody who did not build it, leading with what has changed rather than the static architecture."
---

# Orient a newcomer

Use when anyone asks to understand the product or a part of it — "help me
understand X", "how does X work", "what is X", "I'm new, where do I start".

## The failure this exists to prevent

Answering from structure alone. This product merges hundreds of PRs a
fortnight, so a correct description of how it works, minus what changed, is a
stale snapshot that the reader then plans around. Twice this agent gave an
architecture tour while sign-in had moved and a publish had nearly failed every
save on `/onboarding`.

## Do this

1. `recent_changes(repo, days=14)` — a fortnight minimum, wider for a broad
   ask. **Read `index` first.** It lists every entry's date and title for the
   whole window and is always complete; `entries` carries full bodies for only
   a few. Scan the index for anything about auth, identity, sessions, data
   ownership, a near-miss, or a changed contract, then call again with
   `dates="YYYY-MM-DD,..."` to read those bodies.

   Never describe the bodies you happen to have as the whole period. If you
   could not cover it all, say which dates you did read.
2. `repo_overview(repo)` for the structural frame, and the code graph for any
   specific subsystem named.
3. Rank what changed by consequence, never by visibility:
   - where truth lives moved (auth, identity, sessions, which system owns data)
   - something nearly broke, or stopped being able to break
   - a contract between systems changed
   - visible product changes
   - polish
4. Write it as: what it is → what moved and why it matters to *them* → where to
   look → what will confuse them → an offer to go deeper or draw it.
5. Sources at the end: the tools you called and the identifiers you cited.

## Register

Match the asker. A marketer gets audience, funnel and campaign impact; an
engineer gets file paths. Never open a non-engineer's answer with an internal
name. If a picture would land faster than prose, offer to generate one and
deliver it into the channel — never as a path on a disk they cannot see.
