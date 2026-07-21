# Synthetic demo / eval fixture

A small, hand-labeled test set with known right answers, so we can demo the
tool and measure precision/recall without eyeballing single examples. See
`manifest.json` for the machine-readable version of everything below, and
`scripts/run_eval.py` for the harness that scores against it.

Load it with:

    .venv/bin/python scripts/seed_jurisdictions.py   # prerequisite - real code text
    .venv/bin/python scripts/seed_demo_data.py

Both scripts are idempotent. Then score the current engine/prompts against
both tracks:

    .venv/bin/python scripts/run_eval.py --engine groq

This reports, per track: a standard precision/recall/F1 confusion matrix
against the labeled verdicts below, plus a separate "retrieval recall"
figure - whether the labeled counterpart clause was even inside the
candidate set retrieval produced, so a miss can be traced to retrieval vs.
reasoning instead of guessed at.

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

**HYPOTHESIS / fully synthetic.** This is an invented 4-discipline
mini-project (S-101 structural, M-101 mechanical, A-102 architectural, E-101
electrical) with 2 genuine clashes and 2 clean counterparts, so the checker
and its eval harness have a known-answer fixture, distinct from the larger,
unlabeled `cross_discipline_large/` fixture in this same directory (12
sheets, used for scale/volume testing, not scoring - see
`scripts/seed_large_demo.py` for why the two are kept separate).

Ingested as **Demo: Cross-Discipline Clash Set**, tied to Chicago's
jurisdiction only because a project needs *some* jurisdiction_id - the
jurisdiction is irrelevant to this track. As of 2026-07-21, groq's
end-to-end precision/recall against these labeled pairs is measured by
`scripts/run_eval.py`, not eyeballed.
