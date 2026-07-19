import fitz

from app.clause_extraction import extract_and_store_jurisdiction_clauses
from app.ingest import find_jurisdiction, get_or_create_jurisdiction, ingest_jurisdiction_document, jurisdictions_with_documentation
from app.models import DocType, JurisdictionClause


def make_test_pdf(text: str) -> bytes:
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), text, fontsize=10)
        return doc.tobytes()


def test_get_or_create_jurisdiction_is_case_and_whitespace_insensitive(session):
    j1 = get_or_create_jurisdiction(session, "Loudoun County, VA")
    j2 = get_or_create_jurisdiction(session, " loudoun county, va ")

    assert j1.id == j2.id
    assert find_jurisdiction(session, "LOUDOUN COUNTY, VA").id == j1.id


def test_jurisdictions_with_documentation_excludes_empty_jurisdictions(session, storage):
    empty = get_or_create_jurisdiction(session, "Empty County")
    loaded = get_or_create_jurisdiction(session, "Loaded County")
    ingest_jurisdiction_document(
        session, loaded, title="Building Code", doc_type=DocType.SPEC,
        content=b"1.1 General requirements.", filename="code.txt",
    )

    listed = jurisdictions_with_documentation(session)

    assert loaded.id in [j.id for j in listed]
    assert empty.id not in [j.id for j in listed]


def test_ingest_jurisdiction_document_and_extract_clauses(session, storage):
    jurisdiction = get_or_create_jurisdiction(session, "Fairfax County, VA")
    pdf_bytes = make_test_pdf("1607.1 Live loads shall not be less than specified in Table 1607.1.")

    document = ingest_jurisdiction_document(
        session, jurisdiction, title="Building Code Ch. 16", doc_type=DocType.PDF_2D,
        content=pdf_bytes, filename="code_ch16.pdf",
    )
    count = extract_and_store_jurisdiction_clauses(session, document)

    assert count >= 1
    clauses = session.query(JurisdictionClause).filter_by(jurisdiction_document_id=document.id).all()
    assert any(c.clause_label == "1607.1" for c in clauses)


def test_extract_and_store_jurisdiction_clauses_dedups_on_rerun(session, storage):
    jurisdiction = get_or_create_jurisdiction(session, "Fairfax County, VA")
    document = ingest_jurisdiction_document(
        session, jurisdiction, title="Building Code", doc_type=DocType.SPEC,
        content=b"1.1 First requirement.\n1.2 Second requirement.", filename="code.txt",
    )

    count1 = extract_and_store_jurisdiction_clauses(session, document)
    count2 = extract_and_store_jurisdiction_clauses(session, document)

    total = session.query(JurisdictionClause).filter_by(jurisdiction_document_id=document.id).count()
    assert count1 == count2 == total  # re-running doesn't create duplicates
