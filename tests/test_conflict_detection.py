from app.clause_extraction import extract_and_store_clauses, extract_and_store_jurisdiction_clauses
from app.conflict_detection import check_submission_for_conflicts
from app.ingest import create_submission, get_or_create_jurisdiction, get_or_create_project, ingest_document, ingest_jurisdiction_document
from app.models import DocType, Discipline, Flag, FlagCitation


def test_check_submission_for_conflicts_end_to_end(session, storage):
    jurisdiction = get_or_create_jurisdiction(session, "Test County")
    code_doc = ingest_jurisdiction_document(
        session, jurisdiction, title="Building Code", doc_type=DocType.SPEC,
        content=b"1607.1 Footing depth shall be at least 3 feet below grade.", filename="code.txt",
    )
    extract_and_store_jurisdiction_clauses(session, code_doc)
    session.commit()

    project = get_or_create_project(session, "Test Project", jurisdiction.id)
    submission = create_submission(session, project.id)
    plan_doc = ingest_document(
        session, submission, "S-201", Discipline.STRUCTURAL, "Foundation Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be 5 feet minimum.", filename="s201.txt",
    )
    extract_and_store_clauses(session, plan_doc)
    session.commit()

    flags = check_submission_for_conflicts(session, submission, use_mock=True)
    session.commit()

    assert len(flags) == 1
    assert flags[0].is_simulated is True
    assert flags[0].submission_id == submission.id
    assert len(flags[0].citations) == 1


def test_check_submission_for_conflicts_is_idempotent_on_rerun(session, storage):
    jurisdiction = get_or_create_jurisdiction(session, "Test County")
    code_doc = ingest_jurisdiction_document(
        session, jurisdiction, title="Building Code", doc_type=DocType.SPEC,
        content=b"1607.1 Footing depth shall be at least 3 feet below grade.", filename="code.txt",
    )
    extract_and_store_jurisdiction_clauses(session, code_doc)
    session.commit()

    project = get_or_create_project(session, "Test Project", jurisdiction.id)
    submission = create_submission(session, project.id)
    plan_doc = ingest_document(
        session, submission, "S-201", Discipline.STRUCTURAL, "Foundation Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be 5 feet minimum.", filename="s201.txt",
    )
    extract_and_store_clauses(session, plan_doc)
    session.commit()

    check_submission_for_conflicts(session, submission, use_mock=True)
    session.commit()
    check_submission_for_conflicts(session, submission, use_mock=True)
    session.commit()

    assert session.query(Flag).filter_by(submission_id=submission.id).count() == 1
    assert session.query(FlagCitation).count() == 1


def test_check_submission_for_conflicts_no_flag_when_consistent(session, storage):
    jurisdiction = get_or_create_jurisdiction(session, "Test County")
    code_doc = ingest_jurisdiction_document(
        session, jurisdiction, title="Building Code", doc_type=DocType.SPEC,
        content=b"1607.1 Footing depth shall be at least 3 feet below grade.", filename="code.txt",
    )
    extract_and_store_jurisdiction_clauses(session, code_doc)
    session.commit()

    project = get_or_create_project(session, "Test Project", jurisdiction.id)
    submission = create_submission(session, project.id)
    plan_doc = ingest_document(
        session, submission, "S-201", Discipline.STRUCTURAL, "Foundation Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be at least 3 feet below grade.", filename="s201.txt",
    )
    extract_and_store_clauses(session, plan_doc)
    session.commit()

    flags = check_submission_for_conflicts(session, submission, use_mock=True)
    assert flags == []
