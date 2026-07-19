# Plain English explanation:
# This code is responsible for ingesting files into the database and file storage system. It handles the process of saving file bytes to disk, creating corresponding database entries for submissions, document series, and documents, and managing metadata associated with these files. The code also includes functions for inferring document types based on file extensions and ensuring that projects and submissions are properly created or retrieved from the database.

"""Wires file storage to the document/revision schema.

Scope: gets a file's bytes onto disk and creates the corresponding
Submission/DocumentSeries/Document rows, or the lighter Jurisdiction/
JurisdictionDocument rows for code reference material. Clause extraction
(splitting a document's text into Clause/JurisdictionClause rows) is a
separate concern - see app/clause_extraction.py, called after a
Document/JurisdictionDocument is created.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    DocType,
    Discipline,
    Document,
    DocumentSeries,
    Jurisdiction,
    JurisdictionDocument,
    Project,
    Submission,
)
from app.storage import LocalFileStorage

# initiate local file storage backend for saving files
storage = LocalFileStorage()

# identify different document types based on file extensions
EXTENSION_TO_DOC_TYPE = {
    "pdf": DocType.PDF_2D,
    "rvt": DocType.BIM,
    "ifc": DocType.BIM,
    "nwd": DocType.BIM,
    "nwc": DocType.BIM,
    "doc": DocType.SPEC,
    "docx": DocType.SPEC,
    "txt": DocType.SPEC,
    "rtf": DocType.SPEC,
}

# parses file exteension from file name and assigns corresponding document type, raises error if unrecognized
def infer_doc_type(filename: str) -> DocType:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in EXTENSION_TO_DOC_TYPE:
        recognized = ", ".join(sorted(EXTENSION_TO_DOC_TYPE))
        raise ValueError(f"Can't tell what kind of document '.{ext}' is. Recognized types: {recognized}")
    return EXTENSION_TO_DOC_TYPE[ext]

# finds a project in the database by name, ignoring case and whitespace, returns None if not found
def find_project(session: Session, name: str) -> Project | None:
    """Matches case-/whitespace-insensitively so a stray space or different
    capitalization doesn't miss an existing project."""
    return session.query(Project).filter(func.lower(Project.name) == name.strip().lower()).one_or_none()

# creates a new project in the database if it doesn't already exist, returns the project object
def get_or_create_project(session: Session, name: str, jurisdiction_id: str) -> Project:
    name = name.strip()
    project = find_project(session, name)
    if project is None:
        project = Project(name=name, jurisdiction_id=jurisdiction_id)
        session.add(project)
        session.flush()
    return project


def find_jurisdiction(session: Session, name: str) -> Jurisdiction | None:
    """Case-/whitespace-insensitive, same reasoning as find_project."""
    return session.query(Jurisdiction).filter(
        func.lower(Jurisdiction.name) == name.strip().lower()
    ).one_or_none()


def get_or_create_jurisdiction(session: Session, name: str) -> Jurisdiction:
    name = name.strip()
    jurisdiction = find_jurisdiction(session, name)
    if jurisdiction is None:
        jurisdiction = Jurisdiction(name=name)
        session.add(jurisdiction)
        session.flush()
    return jurisdiction


def jurisdictions_with_documentation(session: Session) -> list[Jurisdiction]:
    """Jurisdictions that actually have at least one reference document loaded -
    this is what the project-creation dropdown should offer, not every
    Jurisdiction row that merely exists."""
    return (
        session.query(Jurisdiction)
        .join(JurisdictionDocument)
        .distinct()
        .order_by(Jurisdiction.name)
        .all()
    )


def ingest_jurisdiction_document(
    session: Session,
    jurisdiction: Jurisdiction,
    title: str,
    doc_type: DocType,
    content: bytes,
    filename: str,
) -> JurisdictionDocument:
    file_uri, file_hash = storage.save(content, filename)
    document = JurisdictionDocument(
        jurisdiction_id=jurisdiction.id,
        title=title,
        doc_type=doc_type,
        file_uri=file_uri,
        file_hash=file_hash,
        metadata_={"original_filename": filename},
    )
    session.add(document)
    session.flush()
    return document

# this will eventually be replaced with a real user from an authentication system, but for now it defaults to "web-upload"
DEFAULT_SUBMITTED_BY = "web-upload"  # placeholder until there's auth to pull a real user from

# adds sequence number to revision label mapping
def sequence_to_revision_label(sequence_number: int) -> str:
    """1 -> 'Rev A', 2 -> 'Rev B', ..., 26 -> 'Rev Z', 27 -> 'Rev AA', ..."""
    n = sequence_number
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"Rev {letters}"

# creates a new submission for a project, increments sequence number and revision label, returns the submission object
def create_submission(session: Session, project_id: str, submitted_by: str = DEFAULT_SUBMITTED_BY) -> Submission:
    """Always starts a brand new revision event for a project, regardless of any
    existing open one. sequence_number and revision_label both auto-increment."""
    last_sequence = session.query(func.max(Submission.sequence_number)).filter_by(
        project_id=project_id
    ).scalar()
    sequence_number = (last_sequence or 0) + 1
    submission = Submission(
        project_id=project_id,
        revision_label=sequence_to_revision_label(sequence_number),
        sequence_number=sequence_number,
        submitted_by=submitted_by,
    )
    session.add(submission)
    session.flush()
    return submission

# retrieves the latest submission for a project or creates a new one if none exists, returns the submission object
def get_latest_or_create_submission(
    session: Session, project_id: str, submitted_by: str = DEFAULT_SUBMITTED_BY
) -> Submission:
    """Uploads default to the project's most recent revision, so a batch of sheets
    uploaded one at a time naturally groups into the same Submission. Advancing to
    a new revision is an explicit action (create_submission), not automatic."""
    submission = (
        session.query(Submission)
        .filter_by(project_id=project_id)
        .order_by(Submission.sequence_number.desc())
        .first()
    )
    if submission is None:
        submission = create_submission(session, project_id, submitted_by)
    return submission

# finds a document series in the database, requires both sheet number and project id as those make up a pk for the related table, returns None if not found
def find_series(session: Session, project_id: str, sheet_number: str) -> DocumentSeries | None:
    return session.query(DocumentSeries).filter_by(
        project_id=project_id, sheet_number=sheet_number
    ).one_or_none()

# creates a new document series if it doesn't already exist, updates title and discipline if it does, returns the document series object
def get_or_create_series(
    session: Session, project_id: str, sheet_number: str, discipline: Discipline, title: str
) -> DocumentSeries:
    """Latest title/discipline always wins, same as every other dedup rule in this
    schema - a re-upload of an existing sheet updates its series metadata rather
    than silently discarding what was typed."""
    series = find_series(session, project_id, sheet_number)
    if series is None:
        series = DocumentSeries(
            project_id=project_id, discipline=discipline, sheet_number=sheet_number, title=title
        )
        session.add(series)
    else:
        series.discipline = discipline
        series.title = title
    session.flush()
    return series

# creates a new document entry in the database for a file that has already been saved to storage, returns the document object
def record_document(
    session: Session,
    submission: Submission,
    series: DocumentSeries,
    doc_type: DocType,
    file_uri: str,
    file_hash: str,
    metadata: dict | None = None,
) -> Document:
    """Creates a Document row for bytes already saved to storage.

    Multiple Document rows (e.g. one per sheet extracted from a bundled PDF)
    can point at the same file_uri - metadata is what distinguishes them
    (e.g. {"page_range": [12, 12]}).
    """
    document = Document(
        document_series_id=series.id,
        submission_id=submission.id,
        doc_type=doc_type,
        file_uri=file_uri,
        file_hash=file_hash,
        metadata_=metadata,
    )
    session.add(document)
    session.flush()
    return document

# convenience wrapper for the simple case: one uploaded file is one sheet (e.g. a spec doc, or a whole BIM model with no per-sheet split)
# will eventually want to make document ingestion more flexible to handle multiple sheets per file, but for now this is a simple case
def ingest_document(
    session: Session,
    submission: Submission,
    sheet_number: str,
    discipline: Discipline,
    title: str,
    doc_type: DocType,
    content: bytes,
    filename: str,
) -> Document:
    """Convenience wrapper for the simple case: one uploaded file is one sheet
    (e.g. a spec doc, or a whole BIM model with no per-sheet split)."""
    series = get_or_create_series(session, submission.project_id, sheet_number, discipline, title)
    file_uri, file_hash = storage.save(content, filename)
    return record_document(
        session, submission, series, doc_type, file_uri, file_hash,
        metadata={"original_filename": filename},
    )
