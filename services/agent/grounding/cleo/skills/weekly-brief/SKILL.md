---
name: weekly-brief
description: "Produce the Nursing Mastery week: what shipped, what is in flight, what is stuck — for a team that was not in the code."
---

# Weekly brief

## Sources, in this order

1. **Shipped** — `recent_changes("nursing-mastery", days=7)` and the same for
   `scrapervault` when the week's work spans both. The CHANGELOG is the record;
   Linear's completed feed is capped and ordered by last-touched, so it is the
   wrong source for "what shipped".
2. **In flight** — Linear `NUR` with `repo:nursing-mastery`. Without that label
   you are reporting ScraperVault work as Nursing Mastery's.
3. **Stuck** — issues that have not changed state in over a week, and anything
   blocked.

## Shape

- Lead with the two or three things that actually matter this week, ranked by
  consequence, in plain language.
- Name the people only where a name helps; most of the board is unassigned and
  saying so once is honest, saying it per-issue is noise.
- End with what needs a decision from a human, if anything does.
- Never invent a date. "In Wave 2, not scheduled" is a complete answer.
