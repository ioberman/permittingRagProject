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

from app.conflict_result import ConflictResult
from app.models import Clause, JurisdictionClause

MODEL_NAME = "mock-keyword-heuristic"
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return set(NUMBER_RE.findall(text))


def detect_conflicts(clause: Clause, candidates: list[JurisdictionClause]) -> list[ConflictResult]:
    if not candidates:
        return []

    top = candidates[0]
    clause_numbers = _numbers(clause.text)
    candidate_numbers = _numbers(top.text)
    if not clause_numbers or not candidate_numbers or (clause_numbers & candidate_numbers):
        return []

    return [
        ConflictResult(
            severity="medium",
            explanation=(
                f"[SIMULATED - keyword heuristic, not real reasoning] "
                f"Clause '{clause.clause_label}' mentions {sorted(clause_numbers)} while the "
                f"closest matching code section '{top.clause_label}' mentions "
                f"{sorted(candidate_numbers)} - numeric values differ, flagged for review."
            ),
            cited_jurisdiction_clause_ids=[top.id],
            model=MODEL_NAME,
            is_simulated=True,
        )
    ]
