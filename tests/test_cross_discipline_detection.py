from app.clause_extraction import extract_and_store_clauses
from app.conflict_detection import check_submission_for_conflicts
from app.cross_discipline_detection import check_submission_for_cross_discipline_conflicts
from app.ingest import create_submission, get_or_create_project, ingest_document
from app.models import Discipline, DocType, Flag, LLMCall


def _seed_two_discipline_submission(session, storage, jurisdiction):
    project = get_or_create_project(session, "Test Project", jurisdiction.id)
    submission = create_submission(session, project.id)

    structural_doc = ingest_document(
        session, submission, "S-101", Discipline.STRUCTURAL, "Structural Notes", DocType.SPEC,
        content=b"1.1 Structural beam provides a minimum clear height of 9 feet 0 inches.",
        filename="s101.txt",
    )
    extract_and_store_clauses(session, structural_doc)

    mechanical_doc = ingest_document(
        session, submission, "M-101", Discipline.MECHANICAL, "Mechanical Notes", DocType.SPEC,
        content=b"1.1 Ductwork shall maintain a minimum clearance of 8 feet 6 inches.",
        filename="m101.txt",
    )
    extract_and_store_clauses(session, mechanical_doc)
    session.commit()

    return submission


def test_check_submission_for_cross_discipline_conflicts_end_to_end(session, storage, jurisdiction):
    submission = _seed_two_discipline_submission(session, storage, jurisdiction)

    flags = check_submission_for_cross_discipline_conflicts(session, submission, engine="mock")
    session.commit()

    assert len(flags) == 2  # mock searches per-clause, so both sides of the pair get flagged
    for flag in flags:
        assert flag.is_simulated is True
        assert flag.llm_call_id is not None
        assert len(flag.citations) == 1
        # cross-discipline citations point at another project Clause, not a jurisdiction clause
        assert flag.citations[0].clause_id is not None
        assert flag.citations[0].jurisdiction_clause_id is None


def test_check_submission_for_cross_discipline_conflicts_is_idempotent_on_rerun(session, storage, jurisdiction):
    submission = _seed_two_discipline_submission(session, storage, jurisdiction)

    check_submission_for_cross_discipline_conflicts(session, submission, engine="mock")
    session.commit()
    check_submission_for_cross_discipline_conflicts(session, submission, engine="mock")
    session.commit()

    assert session.query(Flag).filter_by(submission_id=submission.id).count() == 2


def test_cross_discipline_check_does_not_clobber_jurisdiction_flags(session, storage, jurisdiction):
    """The two check families write Flag/LLMCall rows scoped to the same
    submission_id - each check's clear-before-recheck must only touch its own
    check_type, or running one check would silently wipe out the other's
    results."""
    submission = _seed_two_discipline_submission(session, storage, jurisdiction)

    jurisdiction_flags = check_submission_for_conflicts(session, submission, engine="mock")
    session.commit()
    cross_flags = check_submission_for_cross_discipline_conflicts(session, submission, engine="mock")
    session.commit()

    all_flags = session.query(Flag).filter_by(submission_id=submission.id).all()
    assert len(all_flags) == len(jurisdiction_flags) + len(cross_flags)
    assert session.query(LLMCall).filter_by(submission_id=submission.id).count() >= 2

    # re-running the cross-discipline check again shouldn't touch the jurisdiction flags
    check_submission_for_cross_discipline_conflicts(session, submission, engine="mock")
    session.commit()
    assert session.query(Flag).filter_by(submission_id=submission.id).count() == len(all_flags)
