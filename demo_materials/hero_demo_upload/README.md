# Hero demo upload kit

Three letterhead-style PDF sheets for a fictional project ("Lakeshore
Professional Center - Tenant Improvement") - realistic enough to hand
someone to upload themselves through the real ingest flow, not seeded
directly into the database. Designed to produce **both** flag types this
app detects - see verification status below before treating that as
settled.

## What's in here

| File | Sheet | Discipline |
|---|---|---|
| `A-101.pdf` | Architectural General Notes and Life Safety | Architectural |
| `S-101.pdf` | Structural Framing Notes | Structural |
| `M-101.pdf` | Mechanical Ductwork Notes | Mechanical |

## What will flag, and why

**Jurisdiction check** ("vs. code"): `A-101` note 1.2 specifies a 7'-0"
ceiling height in Corridor 104. Chicago's real adopted amendment (clause
1207.2, already ingested in this app's jurisdiction reference data) requires
not less than 7'-6". Note 1.3 (Corridor 108, 8'-0") is a clean, compliant
twin on purpose, so the check isn't flagging every ceiling note
indiscriminately.

**Cross-discipline check**: `S-101` note 1.1 gives Corridor 210 a structural
clear height of 9'-0"; `M-101` note 1.1 requires 9'-6" of clearance for
ductwork in that same corridor - the duct doesn't fit under the beam. Corridor
214's pair (`S-101` 1.2 / `M-101` 1.2) is a deliberately wide-margin clean
twin (14'-0" beam vs. 8'-6" duct clearance) - tightened after live testing
showed a narrower margin got flagged as a false positive by the real model.

## How to use it

1. Create a project: jurisdiction **Chicago, IL**, any project name.
2. Upload all three PDFs as sheets **in the same revision** (don't click
   "Start new revision" between uploads) - cross-discipline candidates are
   only drawn from clauses in the same submission.
   - `A-101` -> Architectural
   - `S-101` -> Structural
   - `M-101` -> Mechanical
3. Run **Check vs. code** with engine **groq** or **real** (not mock - mock's
   keyword heuristic isn't reliable against real jurisdiction text).
4. Run **Check cross-discipline** with engine **groq** or **real** (not
   preview - preview only shows retrieval candidates, it doesn't judge
   conflict vs. no conflict, so nothing gets saved as a flag).
5. Both should show up on the project's Flags page, each citing the specific
   clause pair responsible.

## Verification status

- **Extraction**: confirmed. All three PDFs extract cleanly through the
  real pipeline with no title-block noise - each clause comes through
  intact, verified directly against `app/clause_extraction.py`, not assumed
  from the visual layout alone.
- **Flag behavior**: confirmed live against Groq (`llama-3.3-70b-versatile`)
  on an earlier version of these notes, with the Corridor 214 control pair
  at a narrower margin (10'-6" beam / 10'-0" duct) - that version correctly
  flagged the jurisdiction violation and the Corridor 210 clash, but the
  real model *also* flagged the Corridor 214 control pair as a false
  positive. The margin was widened to 14'-0" / 8'-6" to fix that
  ambiguity, but a full live re-run against the corrected numbers hasn't
  completed yet (Groq's free-tier daily token limit was hit twice in a row
  during testing). The jurisdiction flag and the Corridor 210 cross-discipline
  flag should be unaffected by this change; the Corridor 214 pair not
  flagging is the one thing not yet re-confirmed live - update this note
  once it has been.
- LLM output isn't deterministic either way - if a re-run ever produces a
  different severity or exact wording, that's expected; what should stay
  consistent is *that* both checks produce at least one real flag.
