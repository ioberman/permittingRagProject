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

Two noise-reduction passes, found necessary against real jurisdiction PDFs
(Chicago's building code produced garbage clauses like label="15" text="15,000"
from table cells before these were added):
  - Detected tables (PyMuPDF's find_tables()) are redacted out of the page
    before text extraction, verified not to drop surrounding prose (redaction
    preserves PyMuPDF's own paragraph-flow formatting for what's left, unlike
    a naive word-position filter which would mangle line wrapping).
  - A minimum-content filter drops any resulting clause with fewer than
    MIN_WORDS real words - catches stray fragments (headers, page numbers,
    non-tabular noise) that table redaction alone doesn't touch.
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
    r"^((?:NOTE|SECTION|DETAIL)?\s*\d+[A-Z]?(?:[-.]\d+)*\.?)\s*[:\-]?\s*(.*)", re.IGNORECASE
)
# [A-Z]?(?:[-.]\d+)* handles chapter-letter-hyphenated section numbers like
# "14B-16-1603" (a real Chicago Building Code convention) alongside plain
# decimal ones like "4.2.1" - without it, "14B-16-1603" truncated to just
# "14" at the first non-digit character, and ~19% of Chicago's clauses ended
# up sharing that one useless label (boundaries were still correct - only
# the label was wrong, verified against real extracted text before this fix).

MAX_CLAUSE_LENGTH = 1500
MIN_WORDS = 2  # deliberately low - table redaction handles the worst noise;
               # this only needs to catch what slips through (bare numbers,
               # single-letter table cells), not clip legitimately terse clauses
WORD_RE = re.compile(r"[A-Za-z]{2,}")

RIGHT_MARGIN_CUTOFF = 0.85  # fraction of page width - see extract_pages()

storage = LocalFileStorage()


def _looks_like_real_content(text: str) -> bool:
    """Rejects fragments that don't look like actual prose - table cell noise
    ("15,000"), bare column headers, stray short fragments. A real clause body
    (even a short one) reads as a sentence; noise doesn't."""
    return len(WORD_RE.findall(text)) >= MIN_WORDS


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

    return [(label, body) for label, body in clauses if _looks_like_real_content(body)]


def extract_pages(content: bytes, doc_type: DocType, filename: str) -> list[str]:
    """Returns one text block per page (PDF) or a single block (spec text)."""
    if doc_type == DocType.PDF_2D:
        with fitz.open(stream=content, filetype="pdf") as doc:
            pages = []
            for page in doc:
                tables = page.find_tables()
                page_area = page.rect.width * page.rect.height
                for table in tables.tables:
                    x0, y0, x1, y1 = table.bbox
                    # find_tables() occasionally misreads a page with dense,
                    # grid-aligned legend/schedule content as one giant
                    # sparse table spanning nearly the whole sheet (seen on
                    # 5/5 real sheets tested: exactly one bogus "table" per
                    # page at 84-86% of page area, engulfing real prose in
                    # its bounding box, vs. every genuine detected table on
                    # those same pages staying under ~2%). Redacting a
                    # false positive that size would wipe the page's actual
                    # content, not just table noise - skip anything
                    # implausibly large for an actual table.
                    if (x1 - x0) * (y1 - y0) > 0.3 * page_area:
                        continue
                    page.add_redact_annot(fitz.Rect(table.bbox))

                # CAD-exported sheets (as opposed to linear code-book PDFs)
                # carry a title-block sidebar - firm names/addresses, phone
                # numbers, revision-stamp dates, sheet/project numbers -
                # positioned in a narrow column along the sheet's right
                # edge. get_text()'s reading order follows the PDF content
                # stream, not visual layout, so that sidebar text interleaves
                # with real body notes; CLAUSE_MARKER's line-start regex then
                # fires on a phone number ("720-213-7550" reads identically
                # to a hierarchical section number) or a street address
                # ("12499 WEST COLFAX AVENUE" easily clears MIN_WORDS) as
                # readily as a real numbered note. Verified against 5 real
                # sheets from seed_data/real_projects (a public bid set) via
                # get_text("blocks") bounding boxes: every sheet has a hard
                # gap with zero content between 71% and 91% of page width -
                # real body/legend text never crosses 71%, title-block
                # content never starts before 91% - so redacting the
                # rightmost margin is safe across every sheet in that set,
                # not just the one it was measured on. RIGHT_MARGIN_CUTOFF
                # sits in the middle of that gap for a safety margin against
                # sheets with a slightly different template.
                margin_x = page.rect.width * RIGHT_MARGIN_CUTOFF
                page.add_redact_annot(fitz.Rect(margin_x, 0, page.rect.width, page.rect.height))
                page.apply_redactions()
                pages.append(page.get_text())
            return pages

    if doc_type == DocType.SPEC:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext == "txt":
            return [content.decode("utf-8", errors="replace")]
        if ext == "docx":
            import io

            docx = DocxFile(io.BytesIO(content))
            return ["\n".join(p.text for p in docx.paragraphs)]

    return []  # unsupported format (bim, .doc, .rtf, ...)


SHEET_LABEL_PATTERN = re.compile(r"\bSHEET\s+([A-Za-z]{1,4}[.\-]?\d{1,3}(?:[.\-]\d{1,3})*[A-Za-z]?)\b", re.IGNORECASE)
SHEET_NUMBER_TOKEN = re.compile(r"^[A-Za-z]{1,4}[.\-]?\d{1,3}(?:[.\-]\d{1,3})*[A-Za-z]?$")


def sniff_sheet_number(content: bytes, doc_type: DocType) -> str | None:
    """Best-effort guess at a sheet's own number (e.g. "A-101") from its
    content, to prefill the upload form instead of requiring it typed by
    hand every time. Two independent signals on the first page, tried in
    order:
      1. The largest-font text shaped like a sheet number - real CAD title
         blocks print it oversized (e.g. "A0.1" at 40pt+), since it's the
         thing a reviewer scans for first. Tried first because it's the more
         reliable signal on real CAD exports.
      2. An explicit "SHEET A-101" label - the convention this project's own
         demo materials use, and a fallback for real sheets whose own number
         isn't the single largest thing on the page. Tried second, not
         first: on a real plan set this text just as often shows up as a
         cross-reference to a DIFFERENT sheet ("see sheet A5.0.X for
         details") as it does the sheet's own number - verified against a
         real page where this label match alone would have silently picked
         a cross-referenced sheet instead of the page's actual number, which
         the largest-font signal got right.
    A filename-based fallback (many CAD exports are literally named after
    the sheet, e.g. "A-101.pdf") is left to the caller - it needs no PDF
    parsing at all. Only PDF sheets carry a title block in the first place,
    so this returns None for spec text.
    """
    if doc_type != DocType.PDF_2D:
        return None
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            if doc.page_count == 0:
                return None
            page = doc[0]

            largest: tuple[float, str] | None = None
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if SHEET_NUMBER_TOKEN.match(text) and (largest is None or span["size"] > largest[0]):
                            largest = (span["size"], text)
            if largest:
                return largest[1].upper()

            label_match = SHEET_LABEL_PATTERN.search(page.get_text())
            if label_match:
                return label_match.group(1).upper()
            return None
    except Exception:
        # Malformed/unusual PDF - this is a convenience prefill, not a
        # required step, so fail quietly and let the user type it in.
        return None


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


def get_or_create_jurisdiction_clause(
    session: Session,
    document: JurisdictionDocument,
    label: str,
    text: str,
    location: dict,
    method: ExtractionMethod,
) -> JurisdictionClause:
    """No DocumentClause-style link table here - each JurisdictionClause belongs
    to exactly one JurisdictionDocument (no revisioning to dedup across yet).

    Public (not module-private) because app/freshness/jurisdiction_sync.py
    reuses this same identity/dedup logic for Municode-scraped content, so a
    section whose text hasn't changed since the last scrape reuses its
    existing row instead of accumulating duplicates."""
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
            location=location,
        )
        session.add(clause)
        session.flush()
    return clause


def extract_and_store_jurisdiction_clauses(
    session: Session,
    document: JurisdictionDocument,
    precomputed_clauses: list[tuple[str, str, int]] | None = None,
) -> int:
    """Same extraction logic as extract_and_store_clauses, storing into
    JurisdictionClause instead of Clause/DocumentClause.

    precomputed_clauses (label, text, page_number) skips extract_pages() and
    split_into_clauses() entirely when given - for scripts/seed_jurisdictions.py,
    whose PDFs are static, checked-in files where re-running find_tables()
    (94% of extraction time - 38s alone on Chicago's 231-page code, measured
    directly) on every fresh deploy's boot is pure waste. Nothing about
    real/uploaded jurisdiction documents uses this - those still always
    extract live, since their content isn't known ahead of time."""
    if document.doc_type not in (DocType.PDF_2D, DocType.SPEC):
        return 0

    method = ExtractionMethod.PDF_TEXT if document.doc_type == DocType.PDF_2D else ExtractionMethod.SPEC_TEXT

    if precomputed_clauses is not None:
        count = 0
        for label, body, page_number in precomputed_clauses:
            get_or_create_jurisdiction_clause(session, document, label, body, {"page": page_number}, method)
            count += 1
        session.flush()
        return count

    content = storage.load(document.file_uri)
    filename = (document.metadata_ or {}).get("original_filename") or document.file_uri.rsplit("/", 1)[-1]
    pages = extract_pages(content, document.doc_type, filename)

    count = 0
    for page_number, page_text in enumerate(pages, start=1):
        for label, body in split_into_clauses(page_text):
            get_or_create_jurisdiction_clause(session, document, label, body, {"page": page_number}, method)
            count += 1

    session.flush()
    return count
