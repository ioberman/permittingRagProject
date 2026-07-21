# Continuous Plan Review Copilot — Project Context

## What this is
An AI agent that pre-flights cross-discipline construction/permit submissions before
they reach reviewers, flags conflicts with explainable citations, and continuously
re-checks the plan set as late changes come in. Goal: turn a ~1-week manual QA/QC
cycle into 2–4 hours and lift first-pass approval rates above the current 55–60%
baseline.

## Who we're building for (ICP — treat as ground truth, don't invent new pain points)
- **Buyer:** Director of QA/QC & Design Compliance at mission-critical GC/EPCs and
  multi-discipline A/E firms delivering data center campuses across multiple
  jurisdictions.
- **Not the buyer:** PE-backed developer/CEO/COO — they feel entitlement and
  political risk more than QA/QC pain, and explicitly point to GCs/A/Es as the
  right buyer. Treat them as a champion/intro path, not the primary user.
- **Core trigger:** rising review volume, a licensed-reviewer capacity ceiling, and
  first-pass approvals stuck around 55–60%.

## The problem, quantified (use these numbers in any UI copy, don't invent new ones)
- Cross-discipline conflicts missed after late design changes cause resubmittals
  costing ~$180K and ~6 weeks per incident.
- Portfolio-level rework costs run ~$1.5–2M/year.
- Today's process is manual, multi-round review across mixed BIM/2D ecosystems with
  unwritten local jurisdictional interpretations, plus cross-team email handoffs
  that lose information.
- Target: compress the ~1-week manual QA cycle to 2–4 hours.

## Product requirements, in priority order
As of 2026-07-21, all six are built - see the note after each. BIM ingest
(part of #1) and a couple of trust/adoption items below are the main
remaining gaps; ask before assuming what's next rather than picking for
yourself.
1. Upload/ingest a plan set (multi-discipline, mixed BIM/2D) — 2D/spec done;
   BIM ingest is still a stub (`app/clause_extraction.py` skips it outright).
2. Cross-discipline conflict detection — done (`app/cross_discipline_detection.py`),
   scored against a labeled fixture via `scripts/run_eval.py`.
3. **Explainable, citation-backed flags — hard requirement, not a nice-to-have.**
   Every flag must point to the specific sheets/clauses involved. Reviewers said an
   unexplained miss would erode trust. — done (`FlagCitation`, forced tool-use).
4. Continuous re-check on change — re-run only affected checks, not a full re-review
   — done (`app/check_persistence.py::run_check`, content-hash-driven).
5. Audit trail/export — timestamped, suitable for lenders/boards/E&O review — done
   (`/projects/<id>/audit-report`, `.csv`).
6. Metrics dashboard — first-pass approval rate, cycle time before/after, rework
   avoided — done as `/metrics`, with an honest scope caveat: it reports flags
   caught, reviewer-confirmed precision, and cycle time, not a fabricated
   approval-rate or dollar-rework figure the app has no data to support.

## Trust and adoption requirements (from customer evidence — do not skip)
- Must support a pilot-alongside-human-review mode (tool output compared against a
  human reviewer's, not blindly trusted). — partial: flags carry a reviewer
  status (open/acknowledged/resolved/false_positive, see `Flag.status`) that
  rolls up into `/metrics`' precision figure, but there's no mechanism yet to
  compare against an independent human review pass, just record disposition
  of the tool's own output.
- Visible data security posture — will be reviewed by legal/security. — not started.
- Must handle a "cold start" — no existing document repository is a known objection.
  — done; the app has never depended on a pre-existing document repository.

## Architecture decisions already made (don't relitigate without a reason)
- **Retrieval unit = clause, not fixed-token chunks.** Citations need to point at
  something a human can check against the actual drawing set, so the chunking
  granularity is driven by what a citation needs to reference, not by convenience.
- **Structured LLM output via forced tool-use**, never free text parsed after the
  fact. The model is constrained to a JSON schema (`report_conflicts`) so citations
  can only reference clause_ids it was actually shown — it can't invent a citation
  to something not in its context window.
- **Retrieval and generation are separate steps.** A cheap/fast method (a local
  sentence-embedding model, `all-MiniLM-L6-v2` run via `fastembed`/ONNX
  Runtime rather than `sentence-transformers`/PyTorch — same weights, same
  output, a fraction of the deploy footprint, see `app/retrieval.py`) narrows
  candidate pairs, both jurisdiction and cross-discipline, before the LLM
  reasons over them — this keeps the LLM from
  having to compare every clause against every other clause (O(n²), doesn't
  scale, wastes tokens).
- **Real vs. simulated must always be labeled clearly**, both in code comments and
  in any UI. `app/llm_mock.py` is a crude keyword-heuristic stand-in for
  `app/llm.py` — it exists so the pipeline is runnable without an API key, not as a
  design for how conflict detection should actually work. Never let a mock's output
  be mistaken for the real model's reasoning.

## Data engineering roadmap — complete as of 2026-07-21
All five items below are done; kept here as a record of what was planned and
delivered, not as an active work list. See "What's actually next" below for
where things stand now.
1. Document/clause storage with revision history — done (`app/models.py`'s
   `Submission`/`DocumentSeries`/`Document`/`Clause` chain).
2. Revision diffing at clause granularity (what changed between Rev B → Rev C)
   — done (`app/check_persistence.py::diff_between_submissions`, `/projects/<id>/diff`).
3. Incremental re-check triggering off that diff — done (`run_check`, skips
   any clause already reasoned about for a given check_type).
4. Observability/logging for every LLM call — done (`LLMCall` model: prompt,
   raw response, tokens, latency, model, logged whether or not it produced a
   flag).
5. Evaluation harness — done (`scripts/run_eval.py`, scored against
   `seed_data/synthetic_demo/manifest.json`; see that fixture's own README
   for current precision/recall numbers).

## What's actually next
Not prescriptive — ask before picking one, this is context for the
conversation, not a queue. As of 2026-07-21, in rough order of what would
most affect a real pilot:
- Real-document extraction quality on CAD-exported sheets — partially fixed
  (see `seed_data/real_projects/README.md`); still poor on pages where a
  sheet's title-block content overlaps the same column as body prose.
- BIM ingestion (part of product requirement #1) — still a stub.
- Auth / multi-user support — every action still attributes to a hardcoded
  placeholder submitter; blocks a team actually using this together, not
  just one person demoing it.
- The remaining trust/adoption gaps above (visible security posture, a true
  independent-reviewer comparison rather than just flag-status tracking).

## Working style
- Propose a rough design/schema before writing code for anything non-trivial —
  treat it like reviewing a colleague's design doc, push back before agreeing.
- Build iteratively, feature by feature, not one large unreviewable drop.
- Ask one focused clarifying question if something is genuinely ambiguous;
  otherwise state an assumption in one line and proceed.
- Flag any invented data point, persona detail, or claim not present in the
  project's customer discovery evidence as a hypothesis, not a fact.

## Model / API notes
- Current models: Claude Sonnet 5 (`claude-sonnet-5`) for reasoning-heavy tasks
  like conflict detection, Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) for
  cheap/high-volume tasks.
- API key is loaded from a `.env` file via `python-dotenv` — never hardcoded,
  never committed (`.env` is in `.gitignore`).
