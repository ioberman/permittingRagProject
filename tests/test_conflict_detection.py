from app.clause_extraction import extract_and_store_clauses, extract_and_store_jurisdiction_clauses
from app.conflict_detection import check_submission_for_conflicts
from app.ingest import create_submission, get_latest_or_create_submission, get_or_create_jurisdiction, get_or_create_project, ingest_document, ingest_jurisdiction_document
from app.models import DocType, Discipline, Flag, FlagCitation, LLMCall


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

    flags = check_submission_for_conflicts(session, submission, engine="mock")
    session.commit()

    assert len(flags) == 1
    assert flags[0].is_simulated is True
    assert flags[0].submission_id == submission.id
    assert len(flags[0].citations) == 1
    assert flags[0].llm_call_id is not None
    assert flags[0].llm_call.model == "mock-keyword-heuristic"
    assert session.query(LLMCall).filter_by(submission_id=submission.id).count() == 1


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

    check_submission_for_conflicts(session, submission, engine="mock")
    session.commit()
    check_submission_for_conflicts(session, submission, engine="mock")
    session.commit()

    assert session.query(Flag).filter_by(submission_id=submission.id).count() == 1
    assert session.query(FlagCitation).count() == 1
    assert session.query(LLMCall).filter_by(submission_id=submission.id).count() == 1


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

    flags = check_submission_for_conflicts(session, submission, engine="mock")
    assert flags == []


def test_check_submission_for_conflicts_still_checks_unrevised_sheets(session, storage):
    """Regression test for the bug where revising ONE sheet made a check
    silently stop reasoning about every other sheet, because it only looked
    at documents tied to the exact new submission_id."""
    jurisdiction = get_or_create_jurisdiction(session, "Test County")
    code_doc = ingest_jurisdiction_document(
        session, jurisdiction, title="Building Code", doc_type=DocType.SPEC,
        content=b"1607.1 Footing depth shall be at least 3 feet below grade.", filename="code.txt",
    )
    extract_and_store_jurisdiction_clauses(session, code_doc)
    session.commit()

    project = get_or_create_project(session, "Test Project", jurisdiction.id)
    submission1 = create_submission(session, project.id)
    structural_doc = ingest_document(
        session, submission1, "S-201", Discipline.STRUCTURAL, "Foundation Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be 5 feet minimum.", filename="s201.txt",
    )
    extract_and_store_clauses(session, structural_doc)
    other_doc = ingest_document(
        session, submission1, "S-202", Discipline.STRUCTURAL, "Other Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be at least 3 feet below grade.", filename="s202.txt",
    )
    extract_and_store_clauses(session, other_doc)
    session.commit()

    # revise ONLY S-201, in a new submission - S-202 isn't re-uploaded
    submission2 = create_submission(session, project.id)
    revised_doc = ingest_document(
        session, submission2, "S-201", Discipline.STRUCTURAL, "Foundation Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be 6 feet minimum.", filename="s201-rev.txt",
    )
    extract_and_store_clauses(session, revised_doc)
    session.commit()

    latest = get_latest_or_create_submission(session, project.id)
    assert latest.id == submission2.id
    flags = check_submission_for_conflicts(session, latest, engine="mock")

    # both sheets' clauses were reasoned over, not just the revised one -
    # both use the same clause label ("4.1"), so this only passes if they're
    # correctly distinguished by document_series_id, not coincidentally equal.
    checked_series = {llm_call.clause.document_series_id for llm_call in
                       session.query(LLMCall).filter_by(submission_id=latest.id).all()}
    assert checked_series == {structural_doc.document_series_id, other_doc.document_series_id}


def _seed_jurisdiction_and_project(session):
    jurisdiction = get_or_create_jurisdiction(session, "Test County")
    code_doc = ingest_jurisdiction_document(
        session, jurisdiction, title="Building Code", doc_type=DocType.SPEC,
        content=b"1607.1 Footing depth shall be at least 3 feet below grade.", filename="code.txt",
    )
    extract_and_store_jurisdiction_clauses(session, code_doc)
    session.commit()
    project = get_or_create_project(session, "Test Project", jurisdiction.id)
    return project


def test_check_submission_for_conflicts_is_incremental_across_revisions(session, storage):
    """A clause that's already been checked (LLMCall exists for this
    check_type) is never reasoned about again, even under a later revision -
    only genuinely new/changed clauses get a new LLMCall."""
    project = _seed_jurisdiction_and_project(session)

    submission1 = create_submission(session, project.id)
    doc1 = ingest_document(
        session, submission1, "S-201", Discipline.STRUCTURAL, "Foundation Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be 5 feet minimum.", filename="s201.txt",
    )
    extract_and_store_clauses(session, doc1)
    session.commit()
    check_submission_for_conflicts(session, submission1, engine="mock")
    session.commit()
    original_llm_call_id = session.query(LLMCall).one().id

    # Rev B: add a brand new sheet, S-201 untouched
    submission2 = create_submission(session, project.id)
    doc2 = ingest_document(
        session, submission2, "S-202", Discipline.STRUCTURAL, "Other Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be at least 3 feet below grade.", filename="s202.txt",
    )
    extract_and_store_clauses(session, doc2)
    session.commit()
    check_submission_for_conflicts(session, submission2, engine="mock")
    session.commit()

    all_calls = session.query(LLMCall).all()
    assert len(all_calls) == 2  # one per sheet, total - not re-run for S-201
    assert original_llm_call_id in {c.id for c in all_calls}  # S-201's original call is untouched
    assert session.query(LLMCall).filter_by(submission_id=submission1.id).count() == 1  # still attached to Rev A
    assert session.query(LLMCall).filter_by(submission_id=submission2.id).count() == 1  # only the new sheet


def test_check_submission_for_conflicts_force_reruns_everything(session, storage):
    project = _seed_jurisdiction_and_project(session)
    submission = create_submission(session, project.id)
    doc = ingest_document(
        session, submission, "S-201", Discipline.STRUCTURAL, "Foundation Plan", DocType.SPEC,
        content=b"4.1 Footing depth shall be 5 feet minimum.", filename="s201.txt",
    )
    extract_and_store_clauses(session, doc)
    session.commit()

    check_submission_for_conflicts(session, submission, engine="mock")
    session.commit()
    first_call_id = session.query(LLMCall).one().id

    check_submission_for_conflicts(session, submission, engine="mock", force=True)
    session.commit()

    calls = session.query(LLMCall).all()
    assert len(calls) == 1  # still just one clause to check
    assert calls[0].id != first_call_id  # but it's a fresh row, not the original
