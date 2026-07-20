"""Loads real-world sheets from seed_data/real_projects/manifest.json into a
single pipeline-test project.

Unlike scripts/seed_demo_data.py, this exists to stress-test extraction
against actual CAD-exported drawing sheets (title blocks, legends, revision
stamps interleaved with body text in non-reading order) rather than to
produce known-answer conflict pairs - there's no jurisdiction documentation
loaded for this project's jurisdiction, so don't run a conflict check against
it expecting meaningful flags.

Usage:
    .venv/bin/python scripts/seed_real_projects.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.clause_extraction import extract_and_store_clauses
from app.db import get_session, init_db
from app.ingest import create_submission, get_or_create_jurisdiction, get_or_create_project, ingest_document
from app.models import Discipline, DocType, Document, DocumentSeries

SEED_DIR = Path(__file__).parent.parent / "seed_data" / "real_projects"
MANIFEST_PATH = SEED_DIR / "manifest.json"


def _already_ingested(session, project_id: str, sheet_number: str) -> bool:
    return (
        session.query(Document)
        .join(DocumentSeries)
        .filter(DocumentSeries.project_id == project_id, DocumentSeries.sheet_number == sheet_number)
        .first()
        is not None
    )


def main():
    init_db()
    session = get_session()
    manifest = json.loads(MANIFEST_PATH.read_text())

    jurisdiction = get_or_create_jurisdiction(session, manifest["jurisdiction"])
    project = get_or_create_project(session, manifest["project_name"], jurisdiction.id)
    submission = create_submission(session, project.id)

    for entry in manifest["documents"]:
        file_path = SEED_DIR / entry["file"]
        if not file_path.exists():
            print(f"SKIP  {entry['sheet_number']}: {entry['file']} not found in {SEED_DIR}")
            continue
        if _already_ingested(session, project.id, entry["sheet_number"]):
            print(f"SKIP  {entry['sheet_number']}: already ingested")
            continue

        document = ingest_document(
            session, submission, entry["sheet_number"], Discipline[entry["discipline"]], entry["title"],
            DocType.PDF_2D, file_path.read_bytes(), entry["file"],
        )
        count = extract_and_store_clauses(session, document)
        session.commit()
        print(f"OK    {entry['sheet_number']:12s} {entry['file']:45s} -> {count} clauses")


if __name__ == "__main__":
    main()
