"""SIMULATED conflict detection - a crude keyword/number heuristic, NOT real
reasoning. Exists so the conflict-detection pipeline is runnable and demoable
without an API key. Never mistake this output for what the real model
(app/llm.py) would conclude - every result is labeled is_simulated=True and
the explanation text is prefixed accordingly.

Heuristic: compares numeric values mentioned in the project clause against
its closest-matching jurisdiction clause (by TF-IDF rank, see app/retrieval.py).
If both mention numbers and none match, flag it - numeric requirements are
the most common form of real code conflict ("4 feet" vs "3 feet minimum").
This will produce false positives/negatives; it's a placeholder, not a model.
"""

import re
import time

from app.conflict_result import CallRecord, ConflictResult
from app.models import Clause, JurisdictionClause

MODEL_NAME = "mock-keyword-heuristic"
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str, label: str) -> set[str]:
    """Numbers mentioned in the clause body, excluding the clause's own
    section-number label (e.g. '4.1') - real extracted clauses include their
    label as the first token of their text, so without this the label itself
    gets mistaken for a substantive number being compared."""
    found = set(NUMBER_RE.findall(text))
    found.discard(label)
    return found


def detect_conflicts(
    clause: Clause, candidates: list[JurisdictionClause]
) -> tuple[list[ConflictResult], CallRecord | None]:
    if not candidates:
        return [], None

    start = time.monotonic()
    top = candidates[0]
    clause_numbers = _numbers(clause.text, clause.clause_label)
    candidate_numbers = _numbers(top.text, top.clause_label)
    latency_ms = int((time.monotonic() - start) * 1000)

    # There's no real "prompt" here - this is a heuristic, not a model call -
    # but recording what was compared and how gives the same audit trail
    # shape as the real engines, so the trace UI works uniformly.
    record = CallRecord(
        prompt=f"Compare numbers in clause '{clause.clause_label}' vs candidate '{top.clause_label}'",
        raw_response=f"clause_numbers={sorted(clause_numbers)} candidate_numbers={sorted(candidate_numbers)}",
        model=MODEL_NAME,
        input_tokens=None,
        output_tokens=None,
        latency_ms=latency_ms,
    )

    if not clause_numbers or not candidate_numbers or (clause_numbers & candidate_numbers):
        return [], record

    results = [
        ConflictResult(
            severity="medium",
            explanation=(
                f"[SIMULATED - keyword heuristic, not real reasoning] "
                f"This clause and the closest matching code section ({top.clause_label}) "
                f"both state numeric requirements, but the numbers don't match: "
                f"this clause says {', '.join(sorted(clause_numbers))}, the code section says "
                f"{', '.join(sorted(candidate_numbers))}. Worth a closer look to confirm "
                f"whether this is a real conflict."
            ),
            cited_candidate_ids=[top.id],
            model=MODEL_NAME,
            is_simulated=True,
        )
    ]
    return results, record


def detect_cross_discipline_conflicts(
    clause: Clause, candidates: list[Clause]
) -> tuple[list[ConflictResult], CallRecord | None]:
    """Same crude numeric-mismatch heuristic as detect_conflicts, applied to
    candidates from other disciplines in the same submission instead of
    jurisdiction code clauses. Just as much a placeholder - real coordination
    clashes (a duct that doesn't fit under a beam) are rarely a same-number
    mismatch, they're a numeric relationship (9'-6" > 9'-0"), which this
    heuristic can't tell from an unrelated pair of numbers."""
    if not candidates:
        return [], None

    start = time.monotonic()
    top = candidates[0]
    clause_numbers = _numbers(clause.text, clause.clause_label)
    candidate_numbers = _numbers(top.text, top.clause_label)
    latency_ms = int((time.monotonic() - start) * 1000)

    record = CallRecord(
        prompt=f"Compare numbers in clause '{clause.clause_label}' vs candidate '{top.clause_label}'",
        raw_response=f"clause_numbers={sorted(clause_numbers)} candidate_numbers={sorted(candidate_numbers)}",
        model=MODEL_NAME,
        input_tokens=None,
        output_tokens=None,
        latency_ms=latency_ms,
    )

    if not clause_numbers or not candidate_numbers or (clause_numbers & candidate_numbers):
        return [], record

    results = [
        ConflictResult(
            severity="medium",
            explanation=(
                f"[SIMULATED - keyword heuristic, not real reasoning] "
                f"This clause and the closest matching clause from another discipline "
                f"({top.clause_label}) both state numeric requirements, but the numbers "
                f"don't match: this clause says {', '.join(sorted(clause_numbers))}, the "
                f"other clause says {', '.join(sorted(candidate_numbers))}. Worth a closer "
                f"look to confirm whether this is a real coordination conflict."
            ),
            cited_candidate_ids=[top.id],
            model=MODEL_NAME,
            is_simulated=True,
        )
    ]
    return results, record
