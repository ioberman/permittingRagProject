"""Loads MVP seed jurisdictions from seed_data/jurisdictions/manifest.json.

manifest.json is a list of {"jurisdiction": ..., "title": ..., "file": ...},
where "file" is a filename in this same directory. Drop real reference
documents there (e.g. local amendment PDFs from Municode/American Legal/a
city site) and list them in the manifest, then run this script:

    .venv/bin/python scripts/seed_jurisdictions.py

Idempotent: re-running skips a (jurisdiction, filename) pair that's already
been ingested, so it's safe to run again after adding new manifest entries.

Extraction results are cached to a .clauses_cache.json file next to each
PDF, keyed by the PDF's own content hash. find_tables() - the table-noise
redaction pass, see app/clause_extraction.py - accounts for 94% of
extraction time on Chicago's 231-page code (38s of 41s total, measured
directly), and these PDFs are static, checked-in files: re-running that on
every fresh deploy's boot (AUTO_SEED_ON_START) is pure waste. The cache is
regenerated automatically if a PDF's content ever actually changes (hash
mismatch), so it can't silently go stale.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.clause_extraction import extract_and_store_jurisdiction_clauses, extract_pages, split_into_clauses
from app.db import get_session, init_db
from app.ingest import get_or_create_jurisdiction, infer_doc_type, ingest_jurisdiction_document
from app.models import JurisdictionDocument

SEED_DIR = Path(__file__).parent.parent / "seed_data" / "jurisdictions"
MANIFEST_PATH = SEED_DIR / "manifest.json"


def already_loaded(session, jurisdiction_id: str, filename: str) -> bool:
    existing = session.query(JurisdictionDocument).filter_by(jurisdiction_id=jurisdiction_id).all()
    return any((d.metadata_ or {}).get("original_filename") == filename for d in existing)


def _cache_path(file_path: Path) -> Path:
    return file_path.with_suffix(file_path.suffix + ".clauses_cache.json")


def _extract_clauses_cached(file_path: Path, content: bytes, doc_type) -> list[tuple[str, str, int]]:
    """Returns [(label, text, page_number), ...], from the on-disk cache if
    its recorded source hash still matches this exact file, otherwise by
    running the real (slow) extraction and writing the cache for next time."""
    source_hash = hashlib.sha256(content).hexdigest()
    cache_path = _cache_path(file_path)

    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached.get("source_sha256") == source_hash:
            return [tuple(c) for c in cached["clauses"]]
        print(f"      cache stale for {file_path.name} (source changed) - re-extracting")

    pages = extract_pages(content, doc_type, file_path.name)
    clauses = [
        (label, body, page_number)
        for page_number, page_text in enumerate(pages, start=1)
        for label, body in split_into_clauses(page_text)
    ]
    cache_path.write_text(json.dumps({"source_sha256": source_hash, "clauses": clauses}))
    return clauses


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
        content = file_path.read_bytes()
        document = ingest_jurisdiction_document(
            session, jurisdiction,
            title=entry["title"], doc_type=doc_type,
            content=content, filename=entry["file"],
        )
        clauses = _extract_clauses_cached(file_path, content, doc_type)
        count = extract_and_store_jurisdiction_clauses(session, document, precomputed_clauses=clauses)
        session.commit()
        print(f"OK    {entry['jurisdiction']!r}: {entry['file']} -> {count} clauses")


if __name__ == "__main__":
    main()
