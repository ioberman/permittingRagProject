"""Orchestrates a cross-discipline coordination check for one submission
(revision): narrows candidates to clauses from OTHER disciplines in the same
submission via embedding similarity, runs the mock or real reasoning engine,
and persists results as Flag/FlagCitation rows citing another project
clause_id instead of a jurisdiction_clause_id. See app/conflict_detection.py
for the other check family (jurisdiction compliance); app/check_persistence.py
holds what's shared between them.

Per CLAUDE.md's HYPOTHESIS labeling convention: this is the first real
consumer of the cross-discipline citation path - FlagCitation.clause_id
existed in the schema from the start for exactly this, but nothing populated
it until now.
"""

from sqlalchemy.orm import Session

from app import llm, llm_groq, llm_mock
from app.check_persistence import current_project_clauses, run_check
from app.models import CheckType, Flag, Submission
from app.retrieval import find_candidate_cross_discipline_clauses

ENGINES = {
    "mock": llm_mock,
    "real": llm,
    "groq": llm_groq,
}


def check_submission_for_cross_discipline_conflicts(
    session: Session, submission: Submission, engine: str = "mock", force: bool = False
) -> list[Flag]:
    clauses = current_project_clauses(session, submission.project_id)
    candidates_by_clause = find_candidate_cross_discipline_clauses(clauses)
    reasoning_engine = ENGINES[engine]

    return run_check(
        session, submission, engine, CheckType.CROSS_DISCIPLINE,
        clauses, candidates_by_clause, reasoning_engine.detect_cross_discipline_conflicts, "clause_id",
        force=force,
    )
