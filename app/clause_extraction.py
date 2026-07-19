"""Splits ingested document text into Clause rows.

Scope: pdf_2d and spec (.txt/.docx) documents only. BIM and legacy .doc/.rtf
spec files are skipped - clause extraction there needs a different approach
(model-element parsing, not text splitting) and isn't built.

Splitting heuristic: a line starting with a numbered marker ("4.2", "4.2.1",
"NOTE 4.2:", "SECTION 3.1.4") starts a new clause; everything up to the next
marker is its body. A page with no markers becomes one clause for the whole
page, so nothing silently disappears. This is a heuristic, not a real
document parser - unusual numbering styles will be missed, and a clause that
spans a page break gets split into two.

If a marker is followed by a long run of text before the next marker (sparse
numbering, or a page with almost no structure), the resulting clause is
capped at MAX_CLAUSE_LENGTH: the overflow is split on paragraph breaks into
labeled sub-clauses ("4.2 (cont. 2)") that still trace back to the same
source marker. A paragraph with no blank lines at all falls back to a hard
character cut, so no single clause is ever unbounded.
"""

import hashlib
import re

import fitz  # PyMuPDF
from docx import Document as DocxFile
from sqlalchemy.orm import Session

from app.models import (
    Clause,
    Document,
    DocumentClause,
    DocType,
    ExtractionMethod,
    JurisdictionClause,
    JurisdictionDocument,
)
from app.storage import LocalFileStorage

CLAUSE_MARKER = re.compile(
    r"^((?:NOTE|SECTION|DETAIL)?\s*\d+(?:\.\d+)*\.?)\s*[:\-]?\s*(.*)", re.IGNORECASE
)

MAX_CLAUSE_LENGTH = 1500

storage = LocalFileStorage()


def _split_oversized(label: str, text: str) -> list[tuple[str, str]]:
    """Splits text over MAX_CLAUSE_LENGTH into labeled sub-clauses, preferring
    paragraph breaks; falls back to a hard character cut if a single
    paragraph is still too long on its own."""
    if len(text) <= MAX_CLAUSE_LENGTH:
        return [(label, text)]

    paragraphs = re.split(r"\n\s*\n", text)
    grouped: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if current and len(candidate) > MAX_CLAUSE_LENGTH:
            grouped.append(current)
            current = para
        else:
            current = candidate
    if current:
        grouped.append(current)

    pieces = [
        chunk[i : i + MAX_CLAUSE_LENGTH]
        for chunk in grouped
        for i in range(0, len(chunk), MAX_CLAUSE_LENGTH)
    ]

    return [
        (label if i == 1 else f"{label} (cont. {i})", piece)
        for i, piece in enumerate(pieces, start=1)
    ]


def split_into_clauses(text: str) -> list[tuple[str, str]]:
    """Returns [(clause_label, clause_text), ...] for one page/block of text."""
    clauses = []
    label = None
    body_lines: list[str] = []

    def flush():
        if label is not None and "\n".join(body_lines).strip():
            clauses.extend(_split_oversized(label, "\n".join(body_lines).strip()))

    for line in text.splitlines():
        stripped = line.strip()
        match = CLAUSE_MARKER.match(stripped) if stripped else None
        if match:
            flush()
            label = match.group(1).strip().rstrip(".")
            body_lines = [stripped]
        elif label is not None:
            body_lines.append(line)

    flush()

    if not clauses and text.strip():
        clauses = _split_oversized("full-text", text.strip())
    return clauses


def extract_pages(content: bytes, doc_type: DocType, filename: str) -> list[str]:
    """Returns one text block per page (PDF) or a single block (spec text)."""
    if doc_type == DocType.PDF_2D:
        with fitz.open(stream=content, filetype="pdf") as doc:
            return [page.get_text() for page in doc]

    if doc_type == DocType.SPEC:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext == "txt":
            return [content.decode("utf-8", errors="replace")]
        if ext == "docx":
            import io

            docx = DocxFile(io.BytesIO(content))
            return ["\n".join(p.text for p in docx.paragraphs)]

    return []  # unsupported format (bim, .doc, .rtf, ...)


def _get_or_link_clause(
    session: Session, document: Document, label: str, text: str, page_number: int, method: ExtractionMethod
) -> Clause:
    content_hash = hashlib.sha256(f"{label}|{text}".encode()).hexdigest()
    clause = session.query(Clause).filter_by(
        document_series_id=document.document_series_id, content_hash=content_hash
    ).one_or_none()
    if clause is None:
        clause = Clause(
            document_series_id=document.document_series_id,
            clause_label=label,
            text=text,
            content_hash=content_hash,
            extraction_method=method,
            location={"page": page_number},
            first_seen_document_id=document.id,
        )
        session.add(clause)
        session.flush()

    already_linked = session.query(DocumentClause).filter_by(
        document_id=document.id, clause_id=clause.id
    ).one_or_none()
    if already_linked is None:
        session.add(DocumentClause(document_id=document.id, clause_id=clause.id))

    return clause


def extract_and_store_clauses(session: Session, document: Document) -> int:
    """Extracts clauses from a Document's file and stores them. Returns the count
    of clauses now linked to this document (existing or newly created)."""
    if document.doc_type not in (DocType.PDF_2D, DocType.SPEC):
        return 0

    content = storage.load(document.file_uri)
    filename = (document.metadata_ or {}).get("original_filename") or document.file_uri.rsplit("/", 1)[-1]
    pages = extract_pages(content, document.doc_type, filename)
    method = ExtractionMethod.PDF_TEXT if document.doc_type == DocType.PDF_2D else ExtractionMethod.SPEC_TEXT

    count = 0
    for page_number, page_text in enumerate(pages, start=1):
        for label, body in split_into_clauses(page_text):
            _get_or_link_clause(session, document, label, body, page_number, method)
            count += 1

    session.flush()
    return count


def _get_or_create_jurisdiction_clause(
    session: Session,
    document: JurisdictionDocument,
    label: str,
    text: str,
    page_number: int,
    method: ExtractionMethod,
) -> JurisdictionClause:
    """No DocumentClause-style link table here - each JurisdictionClause belongs
    to exactly one JurisdictionDocument (no revisioning to dedup across yet)."""
    content_hash = hashlib.sha256(f"{label}|{text}".encode()).hexdigest()
    clause = session.query(JurisdictionClause).filter_by(
        jurisdiction_document_id=document.id, content_hash=content_hash
    ).one_or_none()
    if clause is None:
        clause = JurisdictionClause(
            jurisdiction_document_id=document.id,
            clause_label=label,
            text=text,
            content_hash=content_hash,
            extraction_method=method,
            location={"page": page_number},
        )
        session.add(clause)
        session.flush()
    return clause


def extract_and_store_jurisdiction_clauses(session: Session, document: JurisdictionDocument) -> int:
    """Same extraction logic as extract_and_store_clauses, storing into
    JurisdictionClause instead of Clause/DocumentClause."""
    if document.doc_type not in (DocType.PDF_2D, DocType.SPEC):
        return 0

    content = storage.load(document.file_uri)
    filename = (document.metadata_ or {}).get("original_filename") or document.file_uri.rsplit("/", 1)[-1]
    pages = extract_pages(content, document.doc_type, filename)
    method = ExtractionMethod.PDF_TEXT if document.doc_type == DocType.PDF_2D else ExtractionMethod.SPEC_TEXT

    count = 0
    for page_number, page_text in enumerate(pages, start=1):
        for label, body in split_into_clauses(page_text):
            _get_or_create_jurisdiction_clause(session, document, label, body, page_number, method)
            count += 1

    session.flush()
    return count
