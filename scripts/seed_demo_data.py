"""Loads the contained, hand-labeled demo/eval fixture from
seed_data/synthetic_demo/manifest.json.

Two independent tracks, each idempotent (safe to re-run):

  jurisdiction_conflicts - synthetic project clauses written against REAL
    jurisdiction amendment text already ingested via scripts/seed_jurisdictions.py
    (run that first). One demo project per jurisdiction, since retrieval
    narrows candidates to a single project.jurisdiction_id.

  cross_discipline_conflicts - a fully synthetic 4-discipline mini-project
    with known clash/no-clash pairs. No checker consumes this yet; it exists
    so the cross-discipline checker has a known-answer fixture from day one.

Usage:
    .venv/bin/python scripts/seed_demo_data.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.clause_extraction import extract_and_store_clauses
from app.db import get_session, init_db
from app.ingest import get_latest_or_create_submission, get_or_create_jurisdiction, get_or_create_project, ingest_document
from app.models import Discipline, DocType, Document, DocumentSeries, Project

SEED_DIR = Path(__file__).parent.parent / "seed_data" / "synthetic_demo"
MANIFEST_PATH = SEED_DIR / "manifest.json"


def _already_ingested(session, project_id: str, sheet_number: str) -> bool:
    return (
        session.query(Document)
        .join(DocumentSeries)
        .filter(DocumentSeries.project_id == project_id, DocumentSeries.sheet_number == sheet_number)
        .first()
        is not None
    )


def _ingest_sheet(session, submission, sheet_number, title, discipline, file_path):
    if _already_ingested(session, submission.project_id, sheet_number):
        print(f"SKIP  {sheet_number}: already ingested")
        return
    document = ingest_document(
        session, submission, sheet_number, Discipline[discipline], title,
        DocType.SPEC, file_path.read_bytes(), file_path.name,
    )
    count = extract_and_store_clauses(session, document)
    session.commit()
    print(f"OK    {sheet_number:12s} {file_path.name:45s} -> {count} clauses")


def main():
    init_db()
    session = get_session()
    manifest = json.loads(MANIFEST_PATH.read_text())

    for entry in manifest["jurisdiction_conflicts"]["projects"]:
        jurisdiction = get_or_create_jurisdiction(session, entry["jurisdiction"])
        project = get_or_create_project(session, entry["project_name"], jurisdiction.id)
        submission = get_latest_or_create_submission(session, project.id)
        _ingest_sheet(
            session, submission, entry["sheet_number"], entry["title"], entry["discipline"],
            SEED_DIR / entry["project_file"],
        )

    cross = manifest["cross_discipline_conflicts"]
    jurisdiction = get_or_create_jurisdiction(session, cross["jurisdiction"])
    project = get_or_create_project(session, cross["project_name"], jurisdiction.id)
    # All 4 sheets share ONE submission - cross-discipline candidates are drawn
    # from other clauses in the same submission, so splitting them across
    # separate revisions would make them invisible to each other.
    submission = get_latest_or_create_submission(session, project.id)
    for doc in cross["documents"]:
        _ingest_sheet(session, submission, doc["sheet_number"], doc["title"], doc["discipline"], SEED_DIR / doc["file"])


if __name__ == "__main__":
    main()
