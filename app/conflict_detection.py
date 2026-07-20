"""Orchestrates a jurisdiction-compliance check for one submission (revision):
narrows candidates to jurisdiction code clauses via embedding similarity,
runs the mock or real reasoning engine, and persists results as
Flag/FlagCitation rows citing jurisdiction_clause_id. See
app/cross_discipline_detection.py for the other check family (coordination
conflicts against other project disciplines); app/check_persistence.py holds
what's shared between them, including the incremental-check logic (a clause
is only ever reasoned about once per check_type unless force=True).
"""

from sqlalchemy.orm import Session

from app import llm, llm_groq, llm_mock
from app.check_persistence import current_project_clauses, run_check
from app.models import CheckType, Flag, Submission
from app.retrieval import find_candidate_jurisdiction_clauses

ENGINES = {
    "mock": llm_mock,
    "real": llm,
    "groq": llm_groq,
}


def check_submission_for_conflicts(
    session: Session, submission: Submission, engine: str = "mock", force: bool = False
) -> list[Flag]:
    clauses = current_project_clauses(session, submission.project_id)
    jurisdiction_id = submission.project.jurisdiction_id
    candidates_by_clause = find_candidate_jurisdiction_clauses(session, clauses, jurisdiction_id)
    reasoning_engine = ENGINES[engine]

    return run_check(
        session, submission, engine, CheckType.JURISDICTION,
        clauses, candidates_by_clause, reasoning_engine.detect_conflicts, "jurisdiction_clause_id",
        force=force,
    )
