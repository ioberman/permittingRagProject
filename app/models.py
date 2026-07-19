# Plain English explanation: This code is responsible for defining the database schema for a document management system. It defines the structure of the database tables, including projects, submissions, document series, documents, clauses, and their relationships. The code uses SQLAlchemy to create classes that represent each table and their attributes, as well as enumerations for document types, disciplines, submission statuses, and extraction methods. The schema supports versioning of documents and deduplication of clauses across revisions.

"""Document/clause storage with revision history.

Core identity chain: Project -> Submission (a "Rev A/B/C" upload event) ->
Document (one sheet/spec, versioned per submission) -> Clause (the retrieval
unit, immutable once written).

Clauses are deduplicated by content_hash within a document_series: an
unchanged clause across revisions is the *same row*, referenced by multiple
document versions via document_clauses. This is what makes revision diffing
(added/removed/unchanged) and incremental re-check derivable from the schema
itself, without a separate lineage table.

Jurisdiction -> JurisdictionDocument -> JurisdictionClause is a separate,
lighter-weight parallel chain for jurisdiction code reference material (not
project submissions). No revision-tracking or cross-document dedup there yet -
each jurisdiction document is ingested once, standalone. A Project belongs to
exactly one Jurisdiction.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    ForeignKey,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import Enum as SAEnum


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Discipline(enum.Enum):
    ARCHITECTURAL = "architectural"
    STRUCTURAL = "structural"
    MECHANICAL = "mechanical"
    ELECTRICAL = "electrical"
    PLUMBING = "plumbing"
    CIVIL = "civil"
    FIRE_PROTECTION = "fire_protection"
    LOW_VOLTAGE = "low_voltage"


class DocType(enum.Enum):
    BIM = "bim"
    PDF_2D = "pdf_2d"
    SPEC = "spec"


class SubmissionStatus(enum.Enum):
    PROCESSING = "processing"
    REVIEWED = "reviewed"
    SUPERSEDED = "superseded"


class ExtractionMethod(enum.Enum):
    PDF_TEXT = "pdf_text"
    SPEC_TEXT = "spec_text"
    IFC_PROPERTY = "ifc_property"
    MANUAL = "manual"


class Jurisdiction(Base):
    """A jurisdiction whose building code we hold reference documentation for.

    Deliberately no revision-tracking here (unlike Submission for projects) -
    code adoption cycles are a real future need, but not worth building before
    a single real jurisdiction is loaded. See JurisdictionDocument/Clause.
    """

    __tablename__ = "jurisdictions"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    documents: Mapped[list["JurisdictionDocument"]] = relationship(back_populates="jurisdiction")
    projects: Mapped[list["Project"]] = relationship(back_populates="jurisdiction")


class JurisdictionDocument(Base):
    """One reference document for a jurisdiction (a code book, a local amendment)."""

    __tablename__ = "jurisdiction_documents"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    jurisdiction_id: Mapped[str] = mapped_column(ForeignKey("jurisdictions.id"))
    title: Mapped[str]
    doc_type: Mapped[DocType] = mapped_column(SAEnum(DocType, native_enum=False))
    file_uri: Mapped[str]
    file_hash: Mapped[str]
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(default=_now)

    jurisdiction: Mapped["Jurisdiction"] = relationship(back_populates="documents")
    clauses: Mapped[list["JurisdictionClause"]] = relationship(back_populates="document")


class JurisdictionClause(Base):
    """A citable section of jurisdiction code text. No cross-revision dedup (yet) -
    each row belongs to exactly one JurisdictionDocument, unlike Clause which is
    shared across Document versions via document_clauses."""

    __tablename__ = "jurisdiction_clauses"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    jurisdiction_document_id: Mapped[str] = mapped_column(ForeignKey("jurisdiction_documents.id"))
    clause_label: Mapped[str]
    text: Mapped[str]
    content_hash: Mapped[str]
    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        SAEnum(ExtractionMethod, native_enum=False)
    )
    location: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    document: Mapped["JurisdictionDocument"] = relationship(back_populates="clauses")

    __table_args__ = (
        UniqueConstraint("jurisdiction_document_id", "content_hash", name="uq_jurisdiction_clause_hash"),
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str]
    jurisdiction_id: Mapped[str] = mapped_column(ForeignKey("jurisdictions.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now)

    jurisdiction: Mapped["Jurisdiction"] = relationship(back_populates="projects")
    submissions: Mapped[list["Submission"]] = relationship(back_populates="project")
    document_series: Mapped[list["DocumentSeries"]] = relationship(back_populates="project")


class Submission(Base):
    """One full plan-set upload event, e.g. 'Rev C'."""

    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    revision_label: Mapped[str]
    sequence_number: Mapped[int]  # monotonic; revision_label isn't reliably sortable
    submitted_at: Mapped[datetime] = mapped_column(default=_now)
    submitted_by: Mapped[str]
    status: Mapped[SubmissionStatus] = mapped_column(
        SAEnum(SubmissionStatus, native_enum=False), default=SubmissionStatus.PROCESSING
    )
    created_at: Mapped[datetime] = mapped_column(default=_now)

    project: Mapped["Project"] = relationship(back_populates="submissions")
    documents: Mapped[list["Document"]] = relationship(back_populates="submission")

    __table_args__ = (
        UniqueConstraint("project_id", "sequence_number", name="uq_submission_sequence"),
    )


class DocumentSeries(Base):
    """Logical identity of a sheet/spec across revisions, e.g. 'S-201' stays S-201 forever."""

    __tablename__ = "document_series"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    discipline: Mapped[Discipline] = mapped_column(SAEnum(Discipline, native_enum=False))
    sheet_number: Mapped[str]
    title: Mapped[str]

    project: Mapped["Project"] = relationship(back_populates="document_series")
    documents: Mapped[list["Document"]] = relationship(back_populates="series")
    clauses: Mapped[list["Clause"]] = relationship(back_populates="series")

    __table_args__ = (
        UniqueConstraint("project_id", "sheet_number", name="uq_series_sheet_number"),
    )


class Document(Base):
    """One specific version of a document, tied to one submission."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    document_series_id: Mapped[str] = mapped_column(ForeignKey("document_series.id"))
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"))
    doc_type: Mapped[DocType] = mapped_column(SAEnum(DocType, native_enum=False))
    file_uri: Mapped[str]
    file_hash: Mapped[str]
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(default=_now)

    series: Mapped["DocumentSeries"] = relationship(back_populates="documents")
    submission: Mapped["Submission"] = relationship(back_populates="documents")
    document_clauses: Mapped[list["DocumentClause"]] = relationship(back_populates="document")

    __table_args__ = (
        UniqueConstraint("document_series_id", "submission_id", name="uq_document_version"),
    )


class Clause(Base):
    """The retrieval unit. Immutable once written; reused across revisions when unchanged."""

    __tablename__ = "clauses"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)  # the clause_id cited by the LLM
    document_series_id: Mapped[str] = mapped_column(ForeignKey("document_series.id"))
    clause_label: Mapped[str]  # e.g. "Note 4.2", "Detail 3/S-201" - human-checkable
    text: Mapped[str]
    content_hash: Mapped[str]  # sha256(clause_label + text + location); drives dedup
    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        SAEnum(ExtractionMethod, native_enum=False)
    )
    location: Mapped[dict] = mapped_column(JSON)  # {page,bbox} for 2D, {ifc_guid,level,grid} for BIM
    first_seen_document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now)

    series: Mapped["DocumentSeries"] = relationship(back_populates="clauses")
    document_clauses: Mapped[list["DocumentClause"]] = relationship(back_populates="clause")

    __table_args__ = (
        UniqueConstraint("document_series_id", "content_hash", name="uq_clause_content_hash"),
    )


class DocumentClause(Base):
    """Which clauses are present in a given document version (many-to-many)."""

    __tablename__ = "document_clauses"

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    clause_id: Mapped[str] = mapped_column(ForeignKey("clauses.id"), primary_key=True)

    document: Mapped["Document"] = relationship(back_populates="document_clauses")
    clause: Mapped["Clause"] = relationship(back_populates="document_clauses")
