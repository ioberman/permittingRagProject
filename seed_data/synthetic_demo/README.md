# Synthetic demo / eval fixture

A small, hand-labeled test set with known right answers, so we can demo the
tool and (later) measure precision/recall without eyeballing single examples.
See `manifest.json` for the machine-readable version of everything below.

Load it with:

    .venv/bin/python scripts/seed_jurisdictions.py   # prerequisite - real code text
    .venv/bin/python scripts/seed_demo_data.py

Both scripts are idempotent.

## Track 1: jurisdiction_conflicts

Project clause text is synthetic (written for this fixture), but every cited
jurisdiction clause is **real** amendment text already ingested from
`seed_data/jurisdictions/`. Four scenarios (ceiling height, stair guard
height, stair vertical rise, tree drip-line clearance), each as a
conflict/no-conflict pair, split across two demo projects because retrieval
narrows to one `project.jurisdiction_id`:

- **Demo: Chicago Labeled Conflict Set** — ceiling height, guard height, stair rise (3 pairs)
- **Demo: San Diego Labeled Conflict Set** — tree drip-line clearance (1 pair)

The `jurisdiction_clause_id` values in `manifest.json` are as of the real
`permitting.db` at the time this fixture was written (queried directly, not
guessed) — if jurisdiction docs are ever re-extracted, those UUIDs will
change; re-resolve by `(jurisdiction, jurisdiction_clause_label)` instead.

This track exercises the real pipeline end to end (extraction -> retrieval ->
whichever reasoning engine you pick) and is safe to run with `engine=mock`
for pipeline sanity, but the mock engine's crude number-mismatch heuristic
will not reliably match the expected verdicts here - it's a placeholder, not
a judge. Use `engine=groq` or `engine=real` to actually evaluate whether the
reasoning is correct.

## Track 2: cross_discipline_conflicts

**HYPOTHESIS / fully synthetic.** No cross-discipline checker exists yet
(next item on the roadmap per CLAUDE.md). This is an invented 4-discipline
mini-project (S-101 structural, M-101 mechanical, A-102 architectural, E-101
electrical) with 2 genuine clashes and 2 clean counterparts, so the checker
and its eval harness have a known-answer fixture from day one instead of
starting from nothing.

Ingested as **Demo: Cross-Discipline Clash Set**, tied to Chicago's
jurisdiction only because a project needs *some* jurisdiction_id - the
jurisdiction is irrelevant to this track. Don't read anything into flags
produced by running the existing jurisdiction-conflict `/check` against this
project; that's comparing against the wrong kind of candidate entirely and
is just incidental noise until the real cross-discipline checker exists.
