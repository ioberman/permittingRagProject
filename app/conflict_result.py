"""Shared result shape for both app/llm_mock.py and app/llm.py, so callers
(web.py) can swap one for the other without touching anything downstream."""

from dataclasses import dataclass


@dataclass
class ConflictResult:
    severity: str  # matches FlagSeverity value: "low" | "medium" | "high"
    explanation: str
    cited_jurisdiction_clause_ids: list[str]
    model: str
    is_simulated: bool
