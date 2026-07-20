"""Shared result shapes for app/llm_mock.py, app/llm.py, and app/llm_groq.py,
so callers (app/conflict_detection.py) can swap engines without touching
anything downstream."""

from dataclasses import dataclass


@dataclass
class ConflictResult:
    severity: str  # matches FlagSeverity value: "low" | "medium" | "high"
    explanation: str
    cited_candidate_ids: list[str]  # ids into whichever candidate pool was queried (jurisdiction clauses or project clauses) - caller knows which
    model: str
    is_simulated: bool


@dataclass
class CallRecord:
    """Audit record of one detect_conflicts invocation, regardless of whether
    it produced any ConflictResults - persisted as LLMCall by
    app/conflict_detection.py."""

    prompt: str
    raw_response: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
