"""Evaluation harness: measures retrieval + LLM precision/recall against the
hand-labeled fixture in seed_data/synthetic_demo/manifest.json.

Per CLAUDE.md's data-engineering roadmap: "a small labeled set of
true-conflict / true-non-conflict clause pairs to measure retrieval+LLM
precision/recall as prompts or models change, rather than eyeballing single
examples." This is that harness.

Two independent tracks, scored separately (see manifest.json's own "note"
fields for how each was built):
  jurisdiction     - a project clause vs. a real jurisdiction code clause
  cross_discipline - a project clause vs. another project clause (other
                      discipline), fully synthetic/hypothesis pairs

For each labeled pair this reports two things, kept separate on purpose:
  - retrieval hit: was the labeled counterpart clause actually inside the
    top-N candidate set retrieval produced? A "no_conflict" pair not being
    retrieved is expected and fine (nothing to compare); a "conflict" pair
    not being retrieved is a retrieval-stage failure - the LLM never even
    saw the clause it needed to catch the conflict. Reported as its own
    recall figure so a drop in end-to-end recall can be traced to retrieval
    vs. reasoning without guessing.
  - end-to-end verdict: does the current flag set (after running the real
    check, not just retrieval) contain a flag whose citations connect this
    exact labeled pair? Compared against the label to build a standard
    confusion matrix (precision/recall/F1).

Runs with force=True by default - an eval is meant to score what the
*current* prompts/model actually do, not whatever got cached from a
previous run under different prompts. That means every run makes real API
calls under the groq/real engines; groq is free-tier, real is not (see
--engine below).

Usage:
    .venv/bin/python scripts/run_eval.py [--engine mock|groq|real] [--no-force]

Prerequisite: seed_data/synthetic_demo's projects must already be seeded
(.venv/bin/python scripts/seed_demo_data.py) and, for the jurisdiction
track, the real jurisdiction code documents too
(.venv/bin/python scripts/seed_jurisdictions.py) - this script scores
against them, it doesn't seed them.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.check_persistence import current_project_clauses
from app.conflict_detection import check_submission_for_conflicts
from app.cross_discipline_detection import check_submission_for_cross_discipline_conflicts
from app.db import get_session, init_db
from app.ingest import find_project, get_latest_or_create_submission
from app.models import Clause, DocumentSeries
from app.retrieval import find_candidate_cross_discipline_clauses, find_candidate_jurisdiction_clauses

SEED_DIR = Path(__file__).parent.parent / "seed_data" / "synthetic_demo"
MANIFEST_PATH = SEED_DIR / "manifest.json"


def _find_project_clause(session, project_id, clause_label, sheet_number=None):
    query = session.query(Clause).join(DocumentSeries).filter(
        DocumentSeries.project_id == project_id, Clause.clause_label == clause_label
    )
    if sheet_number:
        query = query.filter(DocumentSeries.sheet_number == sheet_number)
    return query.first()


def _confusion(rows):
    tp = sum(1 for r in rows if r["expected"] == "conflict" and r["predicted"] == "conflict")
    fn = sum(1 for r in rows if r["expected"] == "conflict" and r["predicted"] == "no_conflict")
    fp = sum(1 for r in rows if r["expected"] == "no_conflict" and r["predicted"] == "conflict")
    tn = sum(1 for r in rows if r["expected"] == "no_conflict" and r["predicted"] == "no_conflict")
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None

    conflict_rows = [r for r in rows if r["expected"] == "conflict"]
    retrieval_recall = (
        sum(1 for r in conflict_rows if r["retrieved"]) / len(conflict_rows) if conflict_rows else None
    )
    return {
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "retrieval_recall": retrieval_recall,
    }


def _print_track(name, rows, stats):
    print(f"\n=== {name} ===")
    for r in rows:
        mark = "OK  " if r["expected"] == r["predicted"] else "MISS"
        retrieved = "retrieved" if r["retrieved"] else "NOT retrieved"
        print(f"  [{mark}] {r['scenario']:30s} expected={r['expected']:11s} predicted={r['predicted']:11s} ({retrieved})")
    print(f"  TP={stats['tp']} FN={stats['fn']} FP={stats['fp']} TN={stats['tn']}")

    def fmt(x):
        return f"{x:.2f}" if x is not None else "n/a"

    print(f"  precision={fmt(stats['precision'])}  recall={fmt(stats['recall'])}  f1={fmt(stats['f1'])}"
          f"  retrieval_recall={fmt(stats['retrieval_recall'])}")


def eval_jurisdiction_track(session, manifest, engine, force):
    track = manifest["jurisdiction_conflicts"]
    rows = []

    for entry in track["projects"]:
        project = find_project(session, entry["project_name"])
        if project is None:
            print(f"SKIP jurisdiction track project '{entry['project_name']}' - not seeded, "
                  f"run scripts/seed_demo_data.py first", file=sys.stderr)
            continue
        submission = get_latest_or_create_submission(session, project.id)
        clauses = current_project_clauses(session, project.id)
        candidates_by_clause = find_candidate_jurisdiction_clauses(session, clauses, project.jurisdiction_id)

        flags = check_submission_for_conflicts(session, submission, engine=engine, force=force)
        session.commit()
        cited_pairs = {
            (flag.clause_id, citation.jurisdiction_clause_id)
            for flag in flags for citation in flag.citations
            if citation.jurisdiction_clause_id
        }

        for pair in track["pairs"]:
            if pair["jurisdiction"] != entry["jurisdiction"]:
                continue
            clause = _find_project_clause(session, project.id, pair["project_clause_label"])
            if clause is None:
                print(f"SKIP pair '{pair['scenario']}' - project clause "
                      f"'{pair['project_clause_label']}' not found", file=sys.stderr)
                continue
            target_id = pair["jurisdiction_clause_id"]
            retrieved_ids = {jc.id for jc in candidates_by_clause.get(clause.id, [])}
            rows.append({
                "scenario": pair["scenario"],
                "expected": pair["expected_verdict"],
                "predicted": "conflict" if (clause.id, target_id) in cited_pairs else "no_conflict",
                "retrieved": target_id in retrieved_ids,
            })

    stats = _confusion(rows)
    _print_track("jurisdiction", rows, stats)
    return stats


def eval_cross_discipline_track(session, manifest, engine, force):
    track = manifest["cross_discipline_conflicts"]
    rows = []

    project = find_project(session, track["project_name"])
    if project is None:
        print(f"SKIP cross-discipline track project '{track['project_name']}' - not seeded, "
              f"run scripts/seed_demo_data.py first", file=sys.stderr)
        return _confusion(rows)

    submission = get_latest_or_create_submission(session, project.id)
    clauses = current_project_clauses(session, project.id)
    candidates_by_clause = find_candidate_cross_discipline_clauses(clauses)

    flags = check_submission_for_cross_discipline_conflicts(session, submission, engine=engine, force=force)
    session.commit()
    cited_pairs = set()
    for flag in flags:
        for citation in flag.citations:
            if citation.clause_id:
                cited_pairs.add((flag.clause_id, citation.clause_id))
                cited_pairs.add((citation.clause_id, flag.clause_id))  # undirected for pair-matching

    for pair in track["pairs"]:
        clause_a = _find_project_clause(session, project.id, pair["clause_a"]["label"], pair["clause_a"]["sheet"])
        clause_b = _find_project_clause(session, project.id, pair["clause_b"]["label"], pair["clause_b"]["sheet"])
        if clause_a is None or clause_b is None:
            print(f"SKIP pair '{pair['scenario']}' - clause not found", file=sys.stderr)
            continue
        retrieved_ids = {c.id for c in candidates_by_clause.get(clause_a.id, [])}
        rows.append({
            "scenario": pair["scenario"],
            "expected": pair["expected_verdict"],
            "predicted": "conflict" if (clause_a.id, clause_b.id) in cited_pairs else "no_conflict",
            "retrieved": clause_b.id in retrieved_ids,
        })

    stats = _confusion(rows)
    _print_track("cross_discipline", rows, stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", choices=["mock", "groq", "real"], default="groq")
    parser.add_argument("--no-force", action="store_true", help="reuse existing flags instead of re-running the check")
    args = parser.parse_args()

    init_db()
    session = get_session()
    manifest = json.loads(MANIFEST_PATH.read_text())

    print(f"Running eval with engine={args.engine} force={not args.no_force}")
    j_stats = eval_jurisdiction_track(session, manifest, args.engine, not args.no_force)
    c_stats = eval_cross_discipline_track(session, manifest, args.engine, not args.no_force)
    session.close()

    def fmt(x):
        return f"{x:.2f}" if x is not None else "n/a"

    print("\n=== summary ===")
    print(f"  jurisdiction:      precision={fmt(j_stats['precision'])} recall={fmt(j_stats['recall'])} f1={fmt(j_stats['f1'])}")
    print(f"  cross_discipline:  precision={fmt(c_stats['precision'])} recall={fmt(c_stats['recall'])} f1={fmt(c_stats['f1'])}")


if __name__ == "__main__":
    main()
