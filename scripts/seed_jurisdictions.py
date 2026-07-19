"""Loads MVP seed jurisdictions from seed_data/jurisdictions/manifest.json.

manifest.json is a list of {"jurisdiction": ..., "title": ..., "file": ...},
where "file" is a filename in this same directory. Drop real reference
documents there (e.g. local amendment PDFs from Municode/American Legal/a
city site) and list them in the manifest, then run this script:

    .venv/bin/python scripts/seed_jurisdictions.py

Idempotent: re-running skips a (jurisdiction, filename) pair that's already
been ingested, so it's safe to run again after adding new manifest entries.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.clause_extraction import extract_and_store_jurisdiction_clauses
from app.db import get_session, init_db
from app.ingest import get_or_create_jurisdiction, infer_doc_type, ingest_jurisdiction_document
from app.models import JurisdictionDocument

SEED_DIR = Path(__file__).parent.parent / "seed_data" / "jurisdictions"
MANIFEST_PATH = SEED_DIR / "manifest.json"


def already_loaded(session, jurisdiction_id: str, filename: str) -> bool:
    existing = session.query(JurisdictionDocument).filter_by(jurisdiction_id=jurisdiction_id).all()
    return any((d.metadata_ or {}).get("original_filename") == filename for d in existing)


def main():
    init_db()
    session = get_session()
    manifest = json.loads(MANIFEST_PATH.read_text())

    for entry in manifest:
        file_path = SEED_DIR / entry["file"]
        if not file_path.exists():
            print(f"SKIP  {entry['jurisdiction']!r}: {entry['file']} not found in {SEED_DIR}")
            continue

        jurisdiction = get_or_create_jurisdiction(session, entry["jurisdiction"])
        if already_loaded(session, jurisdiction.id, entry["file"]):
            print(f"SKIP  {entry['jurisdiction']!r}: {entry['file']} already loaded")
            continue

        doc_type = infer_doc_type(entry["file"])
        document = ingest_jurisdiction_document(
            session, jurisdiction,
            title=entry["title"], doc_type=doc_type,
            content=file_path.read_bytes(), filename=entry["file"],
        )
        count = extract_and_store_jurisdiction_clauses(session, document)
        session.commit()
        print(f"OK    {entry['jurisdiction']!r}: {entry['file']} -> {count} clauses")


if __name__ == "__main__":
    main()
