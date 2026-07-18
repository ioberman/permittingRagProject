"""Wires file storage to the document/revision schema.

Scope: gets a file's bytes onto disk and creates the corresponding
Submission/DocumentSeries/Document rows. Does NOT extract clauses from the
file content - that's a separate, larger piece (parsing PDF/BIM text into
Clause rows) not built yet.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DocType, Discipline, Document, DocumentSeries, Project, Submission
from app.storage import LocalFileStorage

storage = LocalFileStorage()

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


def infer_doc_type(filename: str) -> DocType:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in EXTENSION_TO_DOC_TYPE:
        recognized = ", ".join(sorted(EXTENSION_TO_DOC_TYPE))
        raise ValueError(f"Can't tell what kind of document '.{ext}' is. Recognized types: {recognized}")
    return EXTENSION_TO_DOC_TYPE[ext]


def find_project(session: Session, name: str) -> Project | None:
    """Matches case-/whitespace-insensitively so a stray space or different
    capitalization doesn't miss an existing project."""
    return session.query(Project).filter(func.lower(Project.name) == name.strip().lower()).one_or_none()


def get_or_create_project(session: Session, name: str, jurisdiction: str) -> Project:
    name = name.strip()
    project = find_project(session, name)
    if project is None:
        project = Project(name=name, jurisdiction=jurisdiction)
        session.add(project)
        session.flush()
    return project


DEFAULT_SUBMITTED_BY = "web-upload"  # placeholder until there's auth to pull a real user from


def sequence_to_revision_label(sequence_number: int) -> str:
    """1 -> 'Rev A', 2 -> 'Rev B', ..., 26 -> 'Rev Z', 27 -> 'Rev AA', ..."""
    n = sequence_number
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"Rev {letters}"


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


def find_series(session: Session, project_id: str, sheet_number: str) -> DocumentSeries | None:
    return session.query(DocumentSeries).filter_by(
        project_id=project_id, sheet_number=sheet_number
    ).one_or_none()


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
    return record_document(session, submission, series, doc_type, file_uri, file_hash)
