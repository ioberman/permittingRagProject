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
    MUNICODE_SCRAPE = "municode_scrape"  # synthetic JurisdictionDocument fed by
    # app/freshness/jurisdiction_sync.py, not a real upload - kept distinct so
    # the jurisdictions page never shows it as if a human had uploaded it.
    STATE_CODE_PDF = "state_code_pdf"  # same idea, for a state-published code
    # PDF (e.g. Connecticut's state building code) fetched on a schedule by
    # app/freshness/state_code.py - a genuine official/public-record PDF,
    # unlike Municode's unofficial scrape, but still not a human upload.


class SubmissionStatus(enum.Enum):
    PROCESSING = "processing"
    REVIEWED = "reviewed"
    SUPERSEDED = "superseded"


class ExtractionMethod(enum.Enum):
    PDF_TEXT = "pdf_text"
    SPEC_TEXT = "spec_text"
    IFC_PROPERTY = "ifc_property"
    MANUAL = "manual"
    MUNICODE_SCRAPE = "municode_scrape"


class FlagSeverity(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FlagStatus(enum.Enum):
    """A reviewer's disposition of a flag - independent of severity, which is
    the model's own confidence label. Defaults to OPEN so every existing flag
    (and every newly-detected one) starts unreviewed. This is the minimal
    version of "pilot alongside human review": it lets a reviewer record
    whether the tool got it right without building a separate system to
    compare against an independent human review pass."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


class FreshnessSourceKind(enum.Enum):
    MUNICODE = "municode"
    ICC_PUBLIC = "icc_public"
    STATE_CODE_PDF = "state_code_pdf"  # official state-published code PDF -
    # unlike municode/icc_public, jurisdiction_id is required, not optional:
    # a state code applies to every jurisdiction in that state, but this
    # project doesn't model "state" as its own entity, so each jurisdiction
    # that should see it gets its own FreshnessSource row pointed at the
    # same source_ref URL (see app/freshness/seed.py).


class CheckType(enum.Enum):
    """Which candidate pool a Flag/LLMCall came from - jurisdiction code clauses
    or other project clauses. Needed because both checks write Flag/LLMCall
    rows scoped to the same submission_id, and each check's idempotent
    clear-before-recheck must only clear its own rows, not the other check's."""

    JURISDICTION = "jurisdiction"
    CROSS_DISCIPLINE = "cross_discipline"


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


class LLMCall(Base):
    """Audit record of one reasoning-engine invocation - what was actually sent
    and received, regardless of whether it produced a flag. Exists because
    "what did the model see" is exactly what an E&O/legal reviewer asks (per
    CLAUDE.md), and a flag's explanation text alone doesn't answer that. Logged
    for every clause actually reasoned over, including ones that produced zero
    flags - "why didn't this get flagged" is as much an audit question as
    "why did this get flagged".
    """

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"))
    clause_id: Mapped[str] = mapped_column(ForeignKey("clauses.id"))
    check_type: Mapped[CheckType] = mapped_column(SAEnum(CheckType, native_enum=False))
    engine: Mapped[str]  # "mock" | "groq" | "real" - which module handled this
    model: Mapped[str]  # e.g. "mock-keyword-heuristic", "llama-3.3-70b-versatile"
    prompt: Mapped[str]
    raw_response: Mapped[str]
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    latency_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(default=_now)

    submission: Mapped["Submission"] = relationship()
    clause: Mapped["Clause"] = relationship()
    flags: Mapped[list["Flag"]] = relationship(back_populates="llm_call")


class Flag(Base):
    """A detected conflict/compliance issue about one project Clause, raised
    against a specific Submission (re-checking a later revision can produce a
    different set of flags - this is not retroactively applied to old ones).

    is_simulated distinguishes a real model call from the keyword-heuristic
    mock (app/llm_mock.py) - never let the mock's output be mistaken for the
    real model's reasoning, per CLAUDE.md's real-vs-simulated principle.
    """

    __tablename__ = "flags"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"))
    clause_id: Mapped[str] = mapped_column(ForeignKey("clauses.id"))
    llm_call_id: Mapped[str] = mapped_column(ForeignKey("llm_calls.id"))
    check_type: Mapped[CheckType] = mapped_column(SAEnum(CheckType, native_enum=False))
    severity: Mapped[FlagSeverity] = mapped_column(SAEnum(FlagSeverity, native_enum=False))
    explanation: Mapped[str]
    model: Mapped[str]  # e.g. "mock-keyword-heuristic" or "claude-sonnet-5"
    is_simulated: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(default=_now)
    status: Mapped[FlagStatus] = mapped_column(SAEnum(FlagStatus, native_enum=False), default=FlagStatus.OPEN)
    status_note: Mapped[str | None] = mapped_column(nullable=True)
    status_updated_at: Mapped[datetime | None] = mapped_column(nullable=True)

    submission: Mapped["Submission"] = relationship()
    clause: Mapped["Clause"] = relationship()
    llm_call: Mapped["LLMCall"] = relationship(back_populates="flags")
    citations: Mapped[list["FlagCitation"]] = relationship(back_populates="flag")


class FlagCitation(Base):
    """Supporting evidence for a Flag. Exactly one of clause_id/jurisdiction_clause_id
    is set per row - a flag can cite jurisdiction code clauses (code-compliance
    checks) and/or other project clauses (future cross-discipline checks)."""

    __tablename__ = "flag_citations"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    flag_id: Mapped[str] = mapped_column(ForeignKey("flags.id"))
    clause_id: Mapped[str | None] = mapped_column(ForeignKey("clauses.id"), nullable=True)
    jurisdiction_clause_id: Mapped[str | None] = mapped_column(
        ForeignKey("jurisdiction_clauses.id"), nullable=True
    )

    flag: Mapped["Flag"] = relationship(back_populates="citations")
    clause: Mapped["Clause | None"] = relationship()
    jurisdiction_clause: Mapped["JurisdictionClause | None"] = relationship()


class FreshnessSource(Base):
    """One pollable freshness target. MUNICODE sources are jurisdiction-specific
    (a county's local amendment ordinance, e.g. on Municode); ICC_PUBLIC sources
    are not tied to any one jurisdiction (a base model code edition, e.g. IBC,
    applies across every jurisdiction that's adopted it) - jurisdiction_id is
    null for those. POC scope only: public/unofficial sources, not the licensed
    ICC Code Connect / UpCodes APIs a production version would need.
    """

    __tablename__ = "freshness_sources"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    kind: Mapped[FreshnessSourceKind] = mapped_column(SAEnum(FreshnessSourceKind, native_enum=False))
    label: Mapped[str]
    jurisdiction_id: Mapped[str | None] = mapped_column(ForeignKey("jurisdictions.id"), nullable=True)
    source_ref: Mapped[str]
    check_interval_seconds: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(default=_now)

    jurisdiction: Mapped["Jurisdiction | None"] = relationship()
    snapshots: Mapped[list["FreshnessSnapshot"]] = relationship(back_populates="source")
    changes: Mapped[list["FreshnessChange"]] = relationship(back_populates="source")


class FreshnessSnapshot(Base):
    """One fetch of a FreshnessSource's content, hashed and stored via the same
    content-addressable LocalFileStorage used for project/jurisdiction documents."""

    __tablename__ = "freshness_snapshots"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("freshness_sources.id"))
    fetched_at: Mapped[datetime] = mapped_column(default=_now)
    content_hash: Mapped[str]
    raw_content_uri: Mapped[str]

    source: Mapped["FreshnessSource"] = relationship(back_populates="snapshots")


class FreshnessChange(Base):
    """A detected content change between two consecutive snapshots of a source.
    prev_snapshot_id is null for a source's first-ever snapshot - there's
    nothing to have changed from yet, so no row is written in that case."""

    __tablename__ = "freshness_changes"

    id: Mapped[str] = mapped_column(primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("freshness_sources.id"))
    detected_at: Mapped[datetime] = mapped_column(default=_now)
    prev_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("freshness_snapshots.id"), nullable=True)
    new_snapshot_id: Mapped[str] = mapped_column(ForeignKey("freshness_snapshots.id"))
    summary: Mapped[str]

    source: Mapped["FreshnessSource"] = relationship(back_populates="changes")
