import time

import pytest

from app.check_persistence import current_documents_for_project, current_project_clauses, diff_between_submissions, run_check
from app.clause_extraction import extract_and_store_clauses
from app.ingest import create_submission, get_or_create_project, ingest_document
from app.models import CheckType, Discipline, DocType


def test_current_documents_for_project_carries_forward_unrevised_sheets(session, storage, jurisdiction):
    """Regression test: revising ONE sheet starts a new submission, but every
    other sheet's most recent version must still count as current - it must
    not silently disappear just because it wasn't re-uploaded into that new
    submission too."""
    project = get_or_create_project(session, "Test Project", jurisdiction.id)

    submission1 = create_submission(session, project.id)
    structural_doc = ingest_document(
        session, submission1, "S-101", Discipline.STRUCTURAL, "Structural Notes", DocType.SPEC,
        content=b"1.1 Footing depth shall be 5 feet minimum.", filename="s101.txt",
    )
    extract_and_store_clauses(session, structural_doc)
    mechanical_doc = ingest_document(
        session, submission1, "M-101", Discipline.MECHANICAL, "Mechanical Notes", DocType.SPEC,
        content=b"1.1 Ductwork shall maintain 8 feet 6 inches clearance.", filename="m101.txt",
    )
    extract_and_store_clauses(session, mechanical_doc)
    session.commit()

    # revise ONLY the structural sheet: new submission, only one new Document
    submission2 = create_submission(session, project.id)
    revised_structural_doc = ingest_document(
        session, submission2, "S-101", Discipline.STRUCTURAL, "Structural Notes", DocType.SPEC,
        content=b"1.1 Footing depth shall be 6 feet minimum.", filename="s101-rev.txt",
    )
    extract_and_store_clauses(session, revised_structural_doc)
    session.commit()

    current_docs = current_documents_for_project(session, project.id)
    current_by_series = {d.document_series_id: d for d in current_docs}

    assert len(current_docs) == 2  # still both sheets, not just the revised one
    assert current_by_series[structural_doc.document_series_id].id == revised_structural_doc.id
    assert current_by_series[mechanical_doc.document_series_id].id == mechanical_doc.id  # unrevised, carried forward

    clauses = current_project_clauses(session, project.id)
    clause_texts = {c.text for c in clauses}
    assert any("6 feet minimum" in t for t in clause_texts)  # revised content present
    assert not any("5 feet minimum" in t for t in clause_texts)  # superseded content not present
    assert any("Ductwork" in t for t in clause_texts)  # unrevised sheet's clauses still present


def test_current_documents_for_project_empty_project_yields_nothing(session, storage, jurisdiction):
    project = get_or_create_project(session, "Empty Project", jurisdiction.id)
    assert current_documents_for_project(session, project.id) == []
    assert current_project_clauses(session, project.id) == []


def test_diff_between_submissions_finds_added_removed_and_skips_unchanged(session, storage, jurisdiction):
    project = get_or_create_project(session, "Test Project", jurisdiction.id)

    submission1 = create_submission(session, project.id)
    revised_doc = ingest_document(
        session, submission1, "S-101", Discipline.STRUCTURAL, "Structural Notes", DocType.SPEC,
        content=b"1.1 Footing depth shall be 5 feet minimum.", filename="s101.txt",
    )
    extract_and_store_clauses(session, revised_doc)
    unchanged_doc = ingest_document(
        session, submission1, "M-101", Discipline.MECHANICAL, "Mechanical Notes", DocType.SPEC,
        content=b"1.1 Ductwork shall maintain 8 feet 6 inches clearance.", filename="m101.txt",
    )
    extract_and_store_clauses(session, unchanged_doc)
    session.commit()

    # Rev B: revise S-101 only, M-101 untouched, a brand new sheet E-101 added
    submission2 = create_submission(session, project.id)
    new_structural_doc = ingest_document(
        session, submission2, "S-101", Discipline.STRUCTURAL, "Structural Notes", DocType.SPEC,
        content=b"1.1 Footing depth shall be 6 feet minimum.", filename="s101-rev.txt",
    )
    extract_and_store_clauses(session, new_structural_doc)
    new_sheet_doc = ingest_document(
        session, submission2, "E-101", Discipline.ELECTRICAL, "Electrical Notes", DocType.SPEC,
        content=b"1.1 Panel EP-1 requires 36 inch clearance.", filename="e101.txt",
    )
    extract_and_store_clauses(session, new_sheet_doc)
    session.commit()

    diff = diff_between_submissions(session, project.id, submission1.sequence_number, submission2.sequence_number)
    by_sheet = {d["series"].sheet_number: d for d in diff}

    assert "M-101" not in by_sheet  # unchanged sheet omitted entirely

    assert "S-101" in by_sheet
    assert not by_sheet["S-101"]["is_new_sheet"]
    assert [c.text for c in by_sheet["S-101"]["added"]] == ["1.1 Footing depth shall be 6 feet minimum."]
    assert [c.text for c in by_sheet["S-101"]["removed"]] == ["1.1 Footing depth shall be 5 feet minimum."]

    assert "E-101" in by_sheet
    assert by_sheet["E-101"]["is_new_sheet"]
    assert len(by_sheet["E-101"]["added"]) == 1
    assert by_sheet["E-101"]["removed"] == []


class _FakeClause:
    """run_check only ever touches clause.id before the concurrent detect_fn
    calls - real Clause rows aren't needed to exercise its executor logic."""

    def __init__(self, clause_id):
        self.id = clause_id


class _FakeRateLimitError(Exception):
    status_code = 429


def test_run_check_fails_fast_on_error_instead_of_waiting_for_every_other_call(session, jurisdiction):
    """Regression test for a real production symptom: a rate-limited engine
    made a check look like it hung forever. The old `with ThreadPoolExecutor
    as executor:` block's __exit__ calls shutdown(wait=True), which blocks
    until every queued clause - including ones that hadn't even started yet -
    finishes its own (possibly slow) call, even after the first failure is
    already known. run_check now cancels anything not yet started and returns
    as soon as the first error is seen, instead of waiting for stragglers."""
    project = get_or_create_project(session, "Rate Limit Test Project", jurisdiction.id)
    submission = create_submission(session, project.id)
    session.commit()

    clauses = [_FakeClause(f"clause-{i}") for i in range(20)]

    def slow_or_raising_detect_fn(clause, candidates):
        if clause.id == "clause-0":
            raise _FakeRateLimitError("rate limited")
        time.sleep(2)  # simulates a real call plus SDK retry/backoff
        return [], None

    start = time.monotonic()
    with pytest.raises(_FakeRateLimitError):
        run_check(
            session,
            submission,
            engine_name="mock",
            check_type=CheckType.JURISDICTION,
            clauses=clauses,
            candidates_by_clause={},
            detect_fn=slow_or_raising_detect_fn,
            citation_field="jurisdiction_clause_id",
        )
    elapsed = time.monotonic() - start

    # Well under the 2s a single slow call takes, let alone the 2s+ each
    # straggler would add if run_check waited for all 20 of them.
    assert elapsed < 1.0
