from pathlib import Path

import fitz

from app.clause_extraction import (
    MAX_CLAUSE_LENGTH,
    extract_and_store_clauses,
    extract_pages,
    split_into_clauses,
)
from app.ingest import create_submission, get_or_create_project, ingest_document
from app.models import Clause, Discipline, DocType, DocumentClause

CHICAGO_CODE_PDF = (
    Path(__file__).parent.parent / "seed_data" / "jurisdictions" / "chicago_il_building_code.pdf"
)


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


def test_split_into_clauses_handles_letter_embedded_section_numbers():
    # Chicago's own numbering scheme ("14B-16-1603") embeds a letter mid-number.
    # Regression test: this used to truncate to just "14" at the first
    # non-digit character, so dozens of distinct sections all shared one label.
    text = (
        "14B-1-001   Adoption of the International Building Code by reference.\n"
        "The IBC is adopted by reference.\n"
        "14B-1-002 Citations.\n"
        "Provisions of IBC may be cited as follows.\n"
    )
    clauses = split_into_clauses(text)

    assert [label for label, _ in clauses] == ["14B-1-001", "14B-1-002"]


def test_split_into_clauses_drops_low_content_fragments():
    # A marker followed by a bare number (typical table-cell noise) shouldn't
    # survive as a clause, even if it happens to match the marker regex.
    text = "4.1 15,000\n4.2 A real note with actual words in it.\n"
    clauses = split_into_clauses(text)

    assert [label for label, _ in clauses] == ["4.2"]


def test_extract_pages_pdf_redacts_detected_tables():
    if not CHICAGO_CODE_PDF.exists():
        import pytest
        pytest.skip("seed PDF not present in this checkout")

    with fitz.open(CHICAGO_CODE_PDF) as doc:
        page = doc[29]  # known to contain a table (verified manually)
        assert page.find_tables().tables, "test assumption: this page has a detectable table"

    pages = extract_pages(CHICAGO_CODE_PDF.read_bytes(), DocType.PDF_2D, "chicago.pdf")
    page_30_text = pages[29]

    # Real prose right after the table survives, with formatting intact.
    assert "9.   Delete Section 406.5.1." in page_30_text
    # The table's own header row (verified present via find_tables().extract()
    # above) is gone from the extracted text.
    assert "TYPE OF CONSTRUCTION" not in page_30_text


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
