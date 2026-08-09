"""Feeds successfully-fetched Municode/state-code-PDF content into the same
JurisdictionClause pool used for real conflict detection (app/retrieval.py),
alongside whatever a user has manually uploaded for the same jurisdiction -
not just a separate freshness dashboard signal that never touches the actual
product pipeline.

Scraped/fetched clauses are additive only: existing rows are matched by
content hash (get_or_create_jurisdiction_clause) and reused when text hasn't
changed, new rows are added when it has. Nothing is ever deleted here - a Flag
can already cite a JurisdictionClause by id (FlagCitation.jurisdiction_clause_id),
and this project's audit-trail requirement (CLAUDE.md) means a citation must
never end up pointing at a row that quietly vanished. A removed/superseded
section's old clause can outlive its removal from the live source - the same
"no revision-tracking yet" tradeoff already accepted for JurisdictionDocument
generally (see app/models.py's docstring), not a new one introduced here.

app/retrieval.py prioritizes whichever source (upload, Municode, or state
code) was most recently confirmed current when ranking near-tied candidates,
via JurisdictionDocument.ingested_at - not "uploaded always wins."
"""

from app.clause_extraction import get_or_create_jurisdiction_clause
from app.freshness import municode as _municode
from app.freshness import state_code as _state_code
from app.models import (
    DocType,
    ExtractionMethod,
    FreshnessSnapshot,
    FreshnessSource,
    FreshnessSourceKind,
    JurisdictionDocument,
)
from app.storage import LocalFileStorage

_DOC_TYPE_BY_KIND = {
    FreshnessSourceKind.MUNICODE: DocType.MUNICODE_SCRAPE,
    FreshnessSourceKind.STATE_CODE_PDF: DocType.STATE_CODE_PDF,
}
_EXTRACTION_METHOD_BY_KIND = {
    FreshnessSourceKind.MUNICODE: ExtractionMethod.MUNICODE_SCRAPE,
    # State code PDFs go through the exact same PDF text extraction as a
    # human-uploaded PDF (app/clause_extraction.py), so PDF_TEXT is accurate
    # here, not a new extraction method - the distinction from a real upload
    # is doc_type/provenance, not how the text was pulled out.
    FreshnessSourceKind.STATE_CODE_PDF: ExtractionMethod.PDF_TEXT,
}


def _extract_sections(source_kind: FreshnessSourceKind, raw: bytes) -> list[tuple[str, str, str]]:
    if source_kind == FreshnessSourceKind.MUNICODE:
        return _municode.extract_sections(raw)
    if source_kind == FreshnessSourceKind.STATE_CODE_PDF:
        return _state_code.extract_sections(raw)
    raise NotImplementedError(f"no clause sync implemented for source kind {source_kind}")


def _get_or_create_scrape_document(
    session, source: FreshnessSource, snapshot: FreshnessSnapshot
) -> JurisdictionDocument:
    """One JurisdictionDocument per FreshnessSource, identified via
    metadata_.freshness_source_id (not a dedicated column - this table
    predates the freshness feature, and adding a nullable JSON key avoids a
    schema migration this project has no tooling for outside create_all()).

    ingested_at is bumped to this snapshot's fetched_at on every sync, whether
    reused or newly created - not just set once at creation. app/retrieval.py
    uses it as "how recently was this confirmed current" to break near-tied
    candidate rankings against user-uploaded clauses. Without this, a scraped
    document that hasn't changed in months would look stale by creation date
    alone, even though the latest check just re-confirmed its content is
    still accurate - the opposite of what "prioritize whichever is newer" is
    supposed to mean."""
    for document in session.query(JurisdictionDocument).filter_by(jurisdiction_id=source.jurisdiction_id).all():
        if (document.metadata_ or {}).get("freshness_source_id") == source.id:
            document.file_uri = snapshot.raw_content_uri
            document.file_hash = snapshot.content_hash
            document.ingested_at = snapshot.fetched_at
            return document

    document = JurisdictionDocument(
        jurisdiction_id=source.jurisdiction_id,
        title=source.label,
        doc_type=_DOC_TYPE_BY_KIND[source.kind],
        file_uri=snapshot.raw_content_uri,
        file_hash=snapshot.content_hash,
        metadata_={"freshness_source_id": source.id},
        ingested_at=snapshot.fetched_at,
    )
    session.add(document)
    session.flush()  # assigns document.id, needed by clause rows below
    return document


def sync_scraped_clauses(
    session, source: FreshnessSource, snapshot: FreshnessSnapshot, storage: LocalFileStorage | None = None
) -> int:
    """Called after every successful MUNICODE/STATE_CODE_PDF run_check, not
    just when a change was detected - get_or_create_jurisdiction_clause is a
    cheap no-op per section when its text hasn't changed. Returns the number
    of sections processed (not necessarily newly created)."""
    if source.jurisdiction_id is None:
        return 0

    storage = storage or LocalFileStorage()
    raw = storage.load(snapshot.raw_content_uri)
    sections = _extract_sections(source.kind, raw)

    document = _get_or_create_scrape_document(session, source, snapshot)
    method = _EXTRACTION_METHOD_BY_KIND[source.kind]
    count = 0
    for doc_id, title, text in sections:
        if not text:
            continue
        get_or_create_jurisdiction_clause(
            session,
            document,
            label=title or doc_id,
            text=text,
            location={"source_doc_id": doc_id},
            method=method,
        )
        count += 1

    session.commit()
    return count
