"""Real conflict-detection reasoning via Groq's free tier (llama-3.3-70b-versatile).

Same interface and citation-grounding discipline as app/llm.py, swapped to a
free hosted provider so this is runnable without paying for the Claude API -
per CLAUDE.md's Claude-first architecture decision, this is a deliberate,
explicit departure for cost reasons during the MVP stage, not a silent
replacement. is_simulated is still False here: this is real model reasoning,
just from a different provider than app/llm.py, not a heuristic placeholder
like app/llm_mock.py.

Groq's API is OpenAI-compatible: tool definitions use {"type": "function",
"function": {...}} and tool call arguments come back as a JSON string that
must be parsed, unlike Anthropic's already-parsed dict.
"""

import json
import os
import time

from groq import Groq

from app.conflict_result import CallRecord, ConflictResult
from app.models import Clause, JurisdictionClause

MODEL_NAME = "llama-3.3-70b-versatile"

REPORT_CONFLICTS_TOOL = {
    "type": "function",
    "function": {
        "name": "report_conflicts",
        "description": (
            "Report code-compliance conflicts between the project clause and the "
            "candidate jurisdiction code clauses provided. Only report a conflict "
            "when the project clause's requirement actually contradicts or fails "
            "to meet the cited code clause - not for merely related topics."
        ),
        "parameters": {
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
                    },
                }
            },
            "required": ["conflicts"],
        },
    },
}

REPORT_CROSS_DISCIPLINE_CONFLICTS_TOOL = {
    "type": "function",
    "function": {
        "name": "report_cross_discipline_conflicts",
        "description": (
            "Report coordination conflicts between this clause and candidate clauses from "
            "OTHER disciplines within the same submission. Only report a conflict when the "
            "two clauses describe the same physical space, element, or system in a way that "
            "is physically or functionally incompatible (e.g. a required clearance that "
            "doesn't fit, a duct routed through a beam's clear height) - not for merely "
            "related topics or clauses that happen to mention the same room or component "
            "without any actual incompatibility."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "conflicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                            "explanation": {"type": "string"},
                            "cited_clause_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["severity", "explanation", "cited_clause_ids"],
                    },
                }
            },
            "required": ["conflicts"],
        },
    },
}

_client: Groq | None = None


def _get_client() -> Groq:
    """Lazy - unlike Anthropic's client, Groq's constructor raises immediately
    if no API key is set. Constructing it at import time would crash the whole
    app on startup for anyone who hasn't configured Groq, even if they only
    ever use the mock or Claude engine."""
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    return _client


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


def detect_conflicts(
    clause: Clause, candidates: list[JurisdictionClause]
) -> tuple[list[ConflictResult], CallRecord | None]:
    if not candidates:
        return [], None

    prompt = _build_prompt(clause, candidates)
    start = time.monotonic()
    response = _get_client().chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        tools=[REPORT_CONFLICTS_TOOL],
        tool_choice={"type": "function", "function": {"name": "report_conflicts"}},
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    usage = response.usage
    record_kwargs = dict(
        prompt=prompt,
        model=MODEL_NAME,
        input_tokens=usage.prompt_tokens if usage else None,
        output_tokens=usage.completion_tokens if usage else None,
        latency_ms=latency_ms,
    )

    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        # A real call was still made and billed - record it, even though it
        # produced no tool call to extract conflicts from.
        record = CallRecord(raw_response=response.choices[0].message.content or "", **record_kwargs)
        return [], record

    arguments = json.loads(tool_calls[0].function.arguments)
    shown_ids = {c.id for c in candidates}
    record = CallRecord(raw_response=json.dumps(arguments), **record_kwargs)

    results = []
    for conflict in arguments["conflicts"]:
        # Defense in depth: only trust citations to clauses actually shown,
        # even though the schema already constrains this to strings.
        cited = [cid for cid in conflict["cited_jurisdiction_clause_ids"] if cid in shown_ids]
        if not cited:
            continue
        results.append(
            ConflictResult(
                severity=conflict["severity"],
                explanation=conflict["explanation"],
                cited_candidate_ids=cited,
                model=MODEL_NAME,
                is_simulated=False,
            )
        )
    return results, record


def _build_cross_discipline_prompt(clause: Clause, candidates: list[Clause]) -> str:
    candidate_text = "\n\n".join(
        f"[id: {c.id}] {c.series.discipline.value} {c.clause_label}: {c.text}" for c in candidates
    )
    return (
        f"Clause '{clause.clause_label}' ({clause.series.discipline.value}):\n{clause.text}\n\n"
        f"Candidate clauses from other disciplines in the same submission (only these may "
        f"be cited):\n{candidate_text}\n\n"
        "Does this clause create a coordination conflict with any of the candidates above - "
        "i.e. do they describe the same physical space, element, or system in a way that is "
        "physically or functionally incompatible? Call report_cross_discipline_conflicts "
        "with your findings, or an empty conflicts list if there's no real conflict."
    )


def detect_cross_discipline_conflicts(
    clause: Clause, candidates: list[Clause]
) -> tuple[list[ConflictResult], CallRecord | None]:
    if not candidates:
        return [], None

    prompt = _build_cross_discipline_prompt(clause, candidates)
    start = time.monotonic()
    response = _get_client().chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        tools=[REPORT_CROSS_DISCIPLINE_CONFLICTS_TOOL],
        tool_choice={"type": "function", "function": {"name": "report_cross_discipline_conflicts"}},
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    usage = response.usage
    record_kwargs = dict(
        prompt=prompt,
        model=MODEL_NAME,
        input_tokens=usage.prompt_tokens if usage else None,
        output_tokens=usage.completion_tokens if usage else None,
        latency_ms=latency_ms,
    )

    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        record = CallRecord(raw_response=response.choices[0].message.content or "", **record_kwargs)
        return [], record

    arguments = json.loads(tool_calls[0].function.arguments)
    shown_ids = {c.id for c in candidates}
    record = CallRecord(raw_response=json.dumps(arguments), **record_kwargs)

    results = []
    for conflict in arguments["conflicts"]:
        # Defense in depth: only trust citations to clauses actually shown,
        # even though the schema already constrains this to strings.
        cited = [cid for cid in conflict["cited_clause_ids"] if cid in shown_ids]
        if not cited:
            continue
        results.append(
            ConflictResult(
                severity=conflict["severity"],
                explanation=conflict["explanation"],
                cited_candidate_ids=cited,
                model=MODEL_NAME,
                is_simulated=False,
            )
        )
    return results, record
