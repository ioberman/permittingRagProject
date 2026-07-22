# Continuous Plan Review Copilot

An AI agent that pre-flights cross-discipline construction/permit submissions
before they reach reviewers. It flags jurisdiction-code violations and
cross-discipline conflicts (structural vs. mechanical, etc.) with
citation-backed explanations, and re-checks the plan set incrementally as
late changes come in.

**Live demo:** [permitting.dev](https://permitting.dev)

## The problem

Cross-discipline plan review at multi-jurisdiction, multi-discipline
construction projects (data center campuses, in particular) is a manual,
multi-round process across mixed BIM/2D drawing sets, with unwritten local
code interpretations and cross-team email handoffs that lose information.
Conflicts missed after late design changes cause resubmittals costing
roughly $180K and 6 weeks per incident, and first-pass approval rates sit
around 55-60%. This tool is aimed at compressing the ~1-week manual QA/QC
cycle down to 2-4 hours by catching those conflicts before submission.

## What it does

1. **Ingest a plan set** — upload multi-discipline PDF/Word documents; the
   app extracts individual clauses (not fixed-size text chunks) so every
   citation can point at something a reviewer can actually check against
   the drawing set.
2. **Detect conflicts** — two independent checks:
   - *Jurisdiction check*: does a clause violate the applicable local code?
   - *Cross-discipline check*: do two disciplines' clauses (e.g. a
     structural beam and a mechanical duct) clash with each other?
3. **Explain every flag** — each flag is forced, via structured LLM tool
   use, to cite the specific clause(s) it's based on. The model can't
   invent a citation to something it wasn't shown.
4. **Re-check on change** — uploading a revised sheet only re-runs checks
   against the clauses that actually changed, not the whole plan set.
5. **Audit trail** — a timestamped, exportable (CSV) record of every flag
   and its reviewer disposition (open/acknowledged/resolved/false positive).
6. **Metrics dashboard** — flags caught, reviewer-confirmed precision, and
   cycle time; deliberately does *not* fabricate a portfolio-level
   dollar-savings figure the app has no data to support.

## Architecture

- **Retrieval unit = clause, not fixed-token chunks.** Chunking granularity
  is driven by what a citation needs to point at, not by convenience.
- **Retrieval and generation are separate steps.** A local sentence-embedding
  model (`all-MiniLM-L6-v2` via `sentence-transformers`) narrows candidate
  clause pairs before the LLM reasons over them, so the LLM never has to
  compare every clause against every other clause.
- **Structured LLM output via forced tool-use**, never free text parsed
  after the fact — citations can only reference clauses the model was
  actually shown.
- **Real vs. simulated is always labeled.** A keyword-heuristic mock engine
  (`app/llm_mock.py`) lets the whole pipeline run with zero API keys; it's
  never presented as real model reasoning.

See [CLAUDE.md](CLAUDE.md) for the full set of architecture decisions and
product requirements this project is built against.

## Stack

Flask + SQLAlchemy (SQLite in dev/prod) + PyMuPDF/python-docx for document
extraction + sentence-transformers for retrieval + Claude (Sonnet 5 /
Haiku 4.5, via Anthropic and Groq) for conflict reasoning.

## Running it locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # add ANTHROPIC_API_KEY / GROQ_API_KEY if you want real LLM checks
python scripts/seed_jurisdictions.py
python scripts/seed_demo_data.py
flask --app app.web run
```

The app runs fully offline with the `mock` engine (no API key required) —
useful for exploring the UI or running the test suite without any secrets
configured.

## Tests

```bash
python -m pytest
```

Also scored against a labeled fixture via `scripts/run_eval.py` for
precision/recall on conflict detection.

## Hosting

Self-hosted on an Oracle Cloud Always Free VM behind nginx + gunicorn, with
HTTPS via Let's Encrypt. See [docs/HOSTING.md](docs/HOSTING.md) for the real,
current ops setup and [docs/DEPLOYMENT_ORACLE.md](docs/DEPLOYMENT_ORACLE.md)
for a from-scratch deployment guide.
