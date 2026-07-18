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
1. Upload/ingest a plan set (multi-discipline, mixed BIM/2D)
2. Cross-discipline conflict detection
3. **Explainable, citation-backed flags — hard requirement, not a nice-to-have.**
   Every flag must point to the specific sheets/clauses involved. Reviewers said an
   unexplained miss would erode trust.
4. Continuous re-check on change — re-run only affected checks, not a full re-review
5. Audit trail/export — timestamped, suitable for lenders/boards/E&O review
6. Metrics dashboard — first-pass approval rate, cycle time before/after, rework
   avoided

## Trust and adoption requirements (from customer evidence — do not skip)
- Must support a pilot-alongside-human-review mode (tool output compared against a
  human reviewer's, not blindly trusted).
- Visible data security posture — will be reviewed by legal/security.
- Must handle a "cold start" — no existing document repository is a known objection.

## Architecture decisions already made (don't relitigate without a reason)
- **Retrieval unit = clause, not fixed-token chunks.** Citations need to point at
  something a human can check against the actual drawing set, so the chunking
  granularity is driven by what a citation needs to reference, not by convenience.
- **Structured LLM output via forced tool-use**, never free text parsed after the
  fact. The model is constrained to a JSON schema (`report_conflicts`) so citations
  can only reference clause_ids it was actually shown — it can't invent a citation
  to something not in its context window.
- **Retrieval and generation are separate steps.** A cheap/fast method (currently
  TF-IDF cosine similarity) narrows candidate cross-discipline pairs before the LLM
  reasons over them — this keeps the LLM from having to compare every clause
  against every other clause (O(n²), doesn't scale, wastes tokens).
- **Real vs. simulated must always be labeled clearly**, both in code comments and
  in any UI. `app/llm_mock.py` is a crude keyword-heuristic stand-in for
  `app/llm.py` — it exists so the pipeline is runnable without an API key, not as a
  design for how conflict detection should actually work. Never let a mock's output
  be mistaken for the real model's reasoning.

## Data engineering roadmap (the current focus — prioritize this work)
In order of leverage:
1. Document/clause storage with revision history — the prerequisite for everything
   below.
2. Revision diffing at clause granularity (what changed between Rev B → Rev C).
3. Incremental re-check triggering off that diff (requirement 4 above).
4. Observability/logging for every LLM call — inputs, outputs, tokens, cost,
   latency, model version. Needed both for cost tracking and because "what did the
   model see" is exactly what an E&O/legal reviewer will ask.
5. Evaluation harness — a small labeled set of true-conflict / true-non-conflict
   clause pairs to measure retrieval+LLM precision/recall as prompts or models
   change, rather than eyeballing single examples.

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
