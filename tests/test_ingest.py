import pytest

from app.clause_extraction import extract_and_store_clauses
from app.ingest import (
    create_submission,
    delete_project_cascade,
    find_project,
    get_latest_or_create_submission,
    get_or_create_project,
    get_or_create_series,
    ingest_document,
    infer_doc_type,
    sequence_to_revision_label,
)
from app.models import (
    Clause,
    CheckType,
    Discipline,
    Document,
    DocumentClause,
    DocumentSeries,
    DocType,
    Flag,
    FlagCitation,
    FlagSeverity,
    LLMCall,
    Project,
    Submission,
)


def test_infer_doc_type_recognized():
    assert infer_doc_type("plan.pdf") == DocType.PDF_2D
    assert infer_doc_type("model.ifc") == DocType.BIM
    assert infer_doc_type("notes.txt") == DocType.SPEC


def test_infer_doc_type_unrecognized_raises():
    with pytest.raises(ValueError):
        infer_doc_type("file.xyz")


def test_get_or_create_project_is_case_and_whitespace_insensitive(session, jurisdiction):
    p1 = get_or_create_project(session, "Acme Campus", jurisdiction.id)
    p2 = get_or_create_project(session, " acme campus ", jurisdiction.id)

    assert p1.id == p2.id
    assert find_project(session, "ACME CAMPUS").id == p1.id


def test_sequence_to_revision_label():
    assert sequence_to_revision_label(1) == "Rev A"
    assert sequence_to_revision_label(26) == "Rev Z"
    assert sequence_to_revision_label(27) == "Rev AA"


def test_create_submission_always_creates_new(session, jurisdiction):
    project = get_or_create_project(session, "Acme", jurisdiction.id)
    s1 = create_submission(session, project.id)
    s2 = create_submission(session, project.id)

    assert s1.id != s2.id
    assert s1.revision_label == "Rev A"
    assert s2.revision_label == "Rev B"


def test_get_latest_or_create_submission_reuses_latest(session, jurisdiction):
    project = get_or_create_project(session, "Acme", jurisdiction.id)
    s1 = get_latest_or_create_submission(session, project.id)
    s2 = get_latest_or_create_submission(session, project.id)
    assert s1.id == s2.id

    s3 = create_submission(session, project.id)
    s4 = get_latest_or_create_submission(session, project.id)
    assert s4.id == s3.id
    assert s4.id != s1.id


def test_get_or_create_series_latest_title_and_discipline_win(session, jurisdiction):
    project = get_or_create_project(session, "Acme", jurisdiction.id)
    series1 = get_or_create_series(session, project.id, "S-201", Discipline.STRUCTURAL, "Foundation Plan")
    series2 = get_or_create_series(
        session, project.id, "S-201", Discipline.CIVIL, "Foundation Plan (revised)"
    )

    assert series1.id == series2.id
    assert series2.discipline == Discipline.CIVIL
    assert series2.title == "Foundation Plan (revised)"


def test_ingest_document_creates_series_and_saves_content(session, storage, jurisdiction):
    project = get_or_create_project(session, "Acme", jurisdiction.id)
    submission = create_submission(session, project.id)

    document = ingest_document(
        session, submission, "S-201", Discipline.STRUCTURAL, "Foundation Plan",
        DocType.PDF_2D, b"pdf bytes", "sheet.pdf",
    )

    assert document.document_series_id is not None
    assert storage.load(document.file_uri) == b"pdf bytes"


def test_ingest_document_dedups_identical_content_across_sheets(session, storage, jurisdiction):
    project = get_or_create_project(session, "Acme", jurisdiction.id)
    submission = create_submission(session, project.id)

    doc1 = ingest_document(
        session, submission, "S-201", Discipline.STRUCTURAL, "Plan A",
        DocType.PDF_2D, b"same bytes", "a.pdf",
    )
    doc2 = ingest_document(
        session, submission, "S-202", Discipline.STRUCTURAL, "Plan B",
        DocType.PDF_2D, b"same bytes", "b.pdf",
    )

    assert doc1.file_uri == doc2.file_uri


def test_delete_project_cascade_removes_everything_and_spares_other_projects(session, storage, jurisdiction):
    project = get_or_create_project(session, "Doomed Project", jurisdiction.id)
    other_project = get_or_create_project(session, "Untouched Project", jurisdiction.id)
    other_submission = create_submission(session, other_project.id)

    submission = create_submission(session, project.id)
    document = ingest_document(
        session, submission, "S-201", Discipline.STRUCTURAL, "Foundation Plan",
        DocType.SPEC, b"1.1 Footing depth shall be 5 feet.", "s201.txt",
    )
    extract_and_store_clauses(session, document)
    session.commit()

    clause = session.query(Clause).filter_by(document_series_id=document.document_series_id).first()
    assert clause is not None

    llm_call = LLMCall(
        submission_id=submission.id, clause_id=clause.id, check_type=CheckType.JURISDICTION,
        engine="mock", model="mock-keyword-heuristic", prompt="p", raw_response="r", latency_ms=1,
    )
    session.add(llm_call)
    session.flush()
    flag = Flag(
        submission_id=submission.id, clause_id=clause.id, llm_call_id=llm_call.id,
        check_type=CheckType.JURISDICTION, severity=FlagSeverity.HIGH, explanation="e",
        model="mock-keyword-heuristic", is_simulated=True,
    )
    session.add(flag)
    session.flush()
    citation = FlagCitation(flag_id=flag.id, clause_id=clause.id)
    session.add(citation)
    session.commit()

    # Captured before deletion - the ORM objects themselves get expired by
    # the commit below, and accessing an attribute on an expired-but-deleted
    # instance raises ObjectDeletedError instead of just reading stale data.
    project_id = project.id
    document_id, clause_id, llm_call_id, flag_id, citation_id = document.id, clause.id, llm_call.id, flag.id, citation.id
    other_project_id, other_submission_id = other_project.id, other_submission.id

    delete_project_cascade(session, project_id)
    session.commit()

    assert session.get(Project, project_id) is None
    assert session.query(Submission).filter_by(project_id=project_id).count() == 0
    assert session.query(DocumentSeries).filter_by(project_id=project_id).count() == 0
    assert session.query(Document).filter_by(id=document_id).count() == 0
    assert session.query(DocumentClause).filter_by(document_id=document_id).count() == 0
    assert session.query(Clause).filter_by(id=clause_id).count() == 0
    assert session.query(LLMCall).filter_by(id=llm_call_id).count() == 0
    assert session.query(Flag).filter_by(id=flag_id).count() == 0
    assert session.query(FlagCitation).filter_by(id=citation_id).count() == 0

    # A second, unrelated project must be completely unaffected.
    assert session.get(Project, other_project_id) is not None
    assert session.get(Submission, other_submission_id) is not None
