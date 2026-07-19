import fitz

from app.clause_extraction import (
    MAX_CLAUSE_LENGTH,
    extract_and_store_clauses,
    extract_pages,
    split_into_clauses,
)
from app.ingest import create_submission, get_or_create_project, ingest_document
from app.models import Clause, Discipline, DocType, DocumentClause


def make_test_pdf(text: str) -> bytes:
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), text, fontsize=10)
        return doc.tobytes()


def test_split_into_clauses_finds_numbered_markers():
    text = "4.1 First note.\nmore text.\n4.2 Second note.\n"
    clauses = split_into_clauses(text)

    assert [label for label, _ in clauses] == ["4.1", "4.2"]
    assert "First note" in clauses[0][1]
    assert "Second note" in clauses[1][1]


def test_split_into_clauses_falls_back_to_full_text_when_no_markers():
    text = "just some prose with no numbering at all"
    assert split_into_clauses(text) == [("full-text", text)]


def test_split_into_clauses_empty_text_yields_nothing():
    assert split_into_clauses("") == []


def test_split_into_clauses_caps_oversized_clause_via_paragraph_breaks():
    # one marker, then several paragraphs, well over MAX_CLAUSE_LENGTH combined
    paragraph = "This is a long paragraph of unstructured spec text. " * 40  # ~2160 chars
    text = f"4.1 Intro line.\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}"

    clauses = split_into_clauses(text)

    assert len(clauses) > 1
    assert clauses[0][0] == "4.1"
    assert all(label == "4.1" or label.startswith("4.1 (cont.") for label, _ in clauses)
    assert all(len(body) <= MAX_CLAUSE_LENGTH for _, body in clauses)


def test_split_into_clauses_hard_cuts_a_single_giant_paragraph():
    # no blank lines at all, so paragraph splitting can't help - must hard-cut
    giant_paragraph = "word " * 1000  # ~5000 chars, one paragraph, no blank lines
    text = f"1) {giant_paragraph}"

    clauses = split_into_clauses(text)

    assert len(clauses) > 1
    assert all(len(body) <= MAX_CLAUSE_LENGTH for _, body in clauses)
    # nothing was silently dropped
    assert sum(len(body) for _, body in clauses) >= len(giant_paragraph)


def test_extract_pages_pdf():
    pdf_bytes = make_test_pdf("4.1 Some note text")
    pages = extract_pages(pdf_bytes, DocType.PDF_2D, "sheet.pdf")

    assert len(pages) == 1
    assert "4.1" in pages[0]


def test_extract_pages_txt():
    assert extract_pages(b"1.1 hello", DocType.SPEC, "spec.txt") == ["1.1 hello"]


def test_extract_pages_unsupported_format_returns_empty():
    assert extract_pages(b"binary junk", DocType.BIM, "model.rvt") == []


def test_extract_and_store_clauses_dedups_across_revisions(session, storage, jurisdiction):
    project = get_or_create_project(session, "Acme", jurisdiction.id)
    pdf_bytes = make_test_pdf("4.1 Footing depth shall be 4 feet minimum.")

    submission1 = create_submission(session, project.id)
    doc1 = ingest_document(
        session, submission1, "S-201", Discipline.STRUCTURAL, "Plan", DocType.PDF_2D, pdf_bytes, "a.pdf"
    )
    count1 = extract_and_store_clauses(session, doc1)
    assert count1 >= 1

    submission2 = create_submission(session, project.id)
    doc2 = ingest_document(
        session, submission2, "S-201", Discipline.STRUCTURAL, "Plan", DocType.PDF_2D, pdf_bytes, "a.pdf"
    )
    count2 = extract_and_store_clauses(session, doc2)

    total_clauses = session.query(Clause).filter_by(document_series_id=doc1.document_series_id).count()
    total_links = session.query(DocumentClause).count()

    assert total_clauses == count1  # no new Clause rows on the second, identical pass
    assert total_links == count1 + count2  # but both documents are linked


def test_extract_and_store_clauses_skips_bim(session, storage, jurisdiction):
    project = get_or_create_project(session, "Acme", jurisdiction.id)
    submission = create_submission(session, project.id)
    document = ingest_document(
        session, submission, "MODEL-1", Discipline.STRUCTURAL, "Model",
        DocType.BIM, b"binary bim data", "model.rvt",
    )

    assert extract_and_store_clauses(session, document) == 0
