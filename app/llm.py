"""Real conflict-detection reasoning via Claude Sonnet 5, per CLAUDE.md's model
notes (claude-sonnet-5 for reasoning-heavy tasks like conflict detection).

Forced tool-use constrains the model to a JSON schema (report_conflicts) so
citations can only reference jurisdiction_clause_ids it was actually shown -
it can't invent a citation to something not in its context window.
"""

import anthropic

from app.conflict_result import ConflictResult
from app.models import Clause, JurisdictionClause

MODEL_NAME = "claude-sonnet-5"

REPORT_CONFLICTS_TOOL = {
    "name": "report_conflicts",
    "description": (
        "Report code-compliance conflicts between the project clause and the "
        "candidate jurisdiction code clauses provided. Only report a conflict "
        "when the project clause's requirement actually contradicts or fails "
        "to meet the cited code clause - not for merely related topics."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "explanation": {"type": "string"},
                        "cited_jurisdiction_clause_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["severity", "explanation", "cited_jurisdiction_clause_ids"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["conflicts"],
        "additionalProperties": False,
    },
    "strict": True,
}

client = anthropic.Anthropic()


def _build_prompt(clause: Clause, candidates: list[JurisdictionClause]) -> str:
    candidate_text = "\n\n".join(f"[id: {c.id}] {c.clause_label}: {c.text}" for c in candidates)
    return (
        f"Project clause '{clause.clause_label}':\n{clause.text}\n\n"
        f"Candidate jurisdiction code clauses (local amendments - only these may "
        f"be cited):\n{candidate_text}\n\n"
        "These candidates are local amendments to the national model building code "
        "(e.g. IBC), not the full code. You may draw on your own general knowledge "
        "of standard model-code provisions to judge whether the project clause is "
        "consistent with normal practice, but any conflict you report must be "
        "grounded in - and cite - one of the local amendment clauses shown above, "
        "since those are the only things a human reviewer can independently verify "
        "against this jurisdiction's actual adopted text.\n\n"
        "Does the project clause conflict with or fail to meet any of these code "
        "requirements? Call report_conflicts with your findings, or an empty "
        "conflicts list if there's no real conflict."
    )


def detect_conflicts(clause: Clause, candidates: list[JurisdictionClause]) -> list[ConflictResult]:
    if not candidates:
        return []

    response = client.messages.create(
        model=MODEL_NAME,
        max_tokens=1024,
        tools=[REPORT_CONFLICTS_TOOL],
        tool_choice={"type": "tool", "name": "report_conflicts"},
        messages=[{"role": "user", "content": _build_prompt(clause, candidates)}],
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    shown_ids = {c.id for c in candidates}

    results = []
    for conflict in tool_use.input["conflicts"]:
        # Defense in depth: only trust citations to clauses actually shown,
        # even though the forced schema already constrains this to strings.
        cited = [cid for cid in conflict["cited_jurisdiction_clause_ids"] if cid in shown_ids]
        if not cited:
            continue
        results.append(
            ConflictResult(
                severity=conflict["severity"],
                explanation=conflict["explanation"],
                cited_jurisdiction_clause_ids=cited,
                model=MODEL_NAME,
                is_simulated=False,
            )
        )
    return results
