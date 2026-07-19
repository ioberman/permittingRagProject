import pytest

from app.ingest import (
    create_submission,
    find_project,
    get_latest_or_create_submission,
    get_or_create_project,
    get_or_create_series,
    ingest_document,
    infer_doc_type,
    sequence_to_revision_label,
)
from app.models import Discipline, DocType


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
