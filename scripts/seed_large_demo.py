"""Loads a larger, multi-discipline demo project for exercising the
cross-discipline checker and the document network graph at a more realistic
scale than the 4-sheet seed_data/synthetic_demo/cross_discipline fixture.

12 sheets across all 8 disciplines (data center campus theme, matching
CLAUDE.md's ICP), with several intentionally-congested rooms/zones (Electrical
Room 210, Data Hall 2, column line 12) that multiple disciplines reference -
some clash, most don't, so retrieval-graph density looks like a real project
instead of the small demo's fully-connected 4-node case.

NOT a labeled eval fixture, unlike seed_data/synthetic_demo/manifest.json -
there's no hand-verified expected_verdict per pair here. This exists purely
for scale/volume testing (checker performance, graph density, UI at more
than a handful of sheets), not for measuring precision/recall. Keep it out
of the eval harness's input set for that reason.

Usage:
    .venv/bin/python scripts/seed_large_demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.clause_extraction import extract_and_store_clauses
from app.db import get_session, init_db
from app.ingest import get_latest_or_create_submission, get_or_create_jurisdiction, get_or_create_project, ingest_document
from app.models import Discipline, DocType, Document, DocumentSeries

SEED_DIR = Path(__file__).parent.parent / "seed_data" / "synthetic_demo" / "cross_discipline_large"
PROJECT_NAME = "Demo: Data Center Campus (Full Set)"
JURISDICTION_NAME = "Chicago, IL"

SHEETS = [
    ("A-101", "Architectural Egress & Room Layout (Demo)", "ARCHITECTURAL"),
    ("A-102", "Architectural Reflected Ceiling Plan (Demo)", "ARCHITECTURAL"),
    ("S-101", "Structural Foundation & Footings (Demo)", "STRUCTURAL"),
    ("S-102", "Structural Steel Framing & Beams (Demo)", "STRUCTURAL"),
    ("M-101", "Mechanical CRAC Unit Layout (Demo)", "MECHANICAL"),
    ("M-102", "Mechanical Ductwork & Louvers (Demo)", "MECHANICAL"),
    ("E-101", "Electrical Switchgear & Panel Layout (Demo)", "ELECTRICAL"),
    ("E-102", "Electrical Conduit & Cable Tray Routing (Demo)", "ELECTRICAL"),
    ("P-101", "Plumbing Domestic Water & Drainage (Demo)", "PLUMBING"),
    ("FP-101", "Fire Protection Sprinkler & Standpipe (Demo)", "FIRE_PROTECTION"),
    ("C-101", "Civil Site Utilities & Grading (Demo)", "CIVIL"),
    ("LV-101", "Low Voltage Structured Cabling & BMS (Demo)", "LOW_VOLTAGE"),
]


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

    jurisdiction = get_or_create_jurisdiction(session, JURISDICTION_NAME)
    project = get_or_create_project(session, PROJECT_NAME, jurisdiction.id)
    # One submission for all sheets - cross-discipline candidates are drawn
    # from other clauses in the same submission, so splitting these across
    # separate revisions would make them invisible to each other.
    submission = get_latest_or_create_submission(session, project.id)

    for sheet_number, title, discipline in SHEETS:
        if _already_ingested(session, project.id, sheet_number):
            print(f"SKIP  {sheet_number}: already ingested")
            continue
        file_path = SEED_DIR / f"{sheet_number}.txt"
        document = ingest_document(
            session, submission, sheet_number, Discipline[discipline], title,
            DocType.SPEC, file_path.read_bytes(), file_path.name,
        )
        count = extract_and_store_clauses(session, document)
        session.commit()
        print(f"OK    {sheet_number:8s} {title:45s} -> {count} clauses")

    session.close()


if __name__ == "__main__":
    main()
