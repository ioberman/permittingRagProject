"""Orchestrates a code-compliance check for one submission (revision):
narrows candidates via TF-IDF, runs the mock or real reasoning engine, and
persists results as Flag/FlagCitation rows.

Re-running a check on the same submission replaces its prior flags rather
than accumulating duplicates - a "check" reflects current state, not history.
"""

from sqlalchemy.orm import Session

from app import llm, llm_mock
from app.models import Clause, Document, DocumentClause, Flag, FlagCitation, FlagSeverity, Submission
from app.retrieval import find_candidate_jurisdiction_clauses


def _clauses_in_submission(session: Session, submission_id: str) -> list[Clause]:
    return (
        session.query(Clause)
        .join(DocumentClause, DocumentClause.clause_id == Clause.id)
        .join(Document, Document.id == DocumentClause.document_id)
        .filter(Document.submission_id == submission_id)
        .distinct()
        .all()
    )


def check_submission_for_conflicts(session: Session, submission: Submission, use_mock: bool = True) -> list[Flag]:
    session.query(FlagCitation).filter(
        FlagCitation.flag_id.in_(
            session.query(Flag.id).filter_by(submission_id=submission.id)
        )
    ).delete(synchronize_session=False)
    session.query(Flag).filter_by(submission_id=submission.id).delete(synchronize_session=False)

    clauses = _clauses_in_submission(session, submission.id)
    jurisdiction_id = submission.project.jurisdiction_id
    candidates_by_clause = find_candidate_jurisdiction_clauses(session, clauses, jurisdiction_id)

    engine = llm_mock if use_mock else llm
    created_flags = []

    for clause in clauses:
        candidates = candidates_by_clause.get(clause.id, [])
        for result in engine.detect_conflicts(clause, candidates):
            flag = Flag(
                submission_id=submission.id,
                clause_id=clause.id,
                severity=FlagSeverity(result.severity),
                explanation=result.explanation,
                model=result.model,
                is_simulated=result.is_simulated,
            )
            session.add(flag)
            session.flush()
            for jurisdiction_clause_id in result.cited_jurisdiction_clause_ids:
                session.add(FlagCitation(flag_id=flag.id, jurisdiction_clause_id=jurisdiction_clause_id))
            created_flags.append(flag)

    session.flush()
    return created_flags
