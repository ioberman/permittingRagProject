"""Tests the parsing/citation-validation logic in app/llm_groq.py without
hitting the real Groq API - the SDK boundary (client.chat.completions.create)
is faked with the exact response shape Groq's OpenAI-compatible API returns."""

import json
from types import SimpleNamespace

import app.llm_groq as llm_groq
from app.models import Clause, Discipline, DocumentSeries, JurisdictionClause


def _clause(text, label="4.1", cid="c1", discipline=Discipline.STRUCTURAL):
    clause = Clause(id=cid, document_series_id="ds1", clause_label=label, text=text,
                     content_hash="h", extraction_method="pdf_text", location={})
    clause.series = DocumentSeries(id="ds1", project_id="p1", discipline=discipline,
                                    sheet_number="S-1", title="Test Sheet")
    return clause


def _jurisdiction_clause(jid, text, label="1607.1"):
    return JurisdictionClause(id=jid, jurisdiction_document_id="jd1", clause_label=label,
                               text=text, content_hash="h2", extraction_method="pdf_text", location={})


def _fake_usage():
    return SimpleNamespace(prompt_tokens=37, completion_tokens=2)


def _fake_response(arguments_dict):
    """Mirrors Groq's OpenAI-compatible response shape: arguments is a JSON
    string, not a parsed object."""
    tool_call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(arguments_dict)))
    message = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=_fake_usage())


class _FakeCompletions:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_FakeCompletions(response))


def test_detect_conflicts_parses_and_returns_result(monkeypatch):
    candidate = _jurisdiction_clause("j1", "Minimum footing depth 3 feet.")
    response = _fake_response({
        "conflicts": [
            {"severity": "high", "explanation": "Depth mismatch.", "cited_jurisdiction_clause_ids": ["j1"]}
        ]
    })
    monkeypatch.setattr(llm_groq, "_get_client", lambda: _FakeClient(response))

    results, record = llm_groq.detect_conflicts(_clause("Footing depth shall be 5 feet."), [candidate])

    assert len(results) == 1
    assert results[0].severity == "high"
    assert results[0].is_simulated is False
    assert results[0].model == llm_groq.MODEL_NAME
    assert results[0].cited_candidate_ids == ["j1"]
    assert record is not None
    assert record.model == llm_groq.MODEL_NAME
    assert record.input_tokens == 37
    assert record.output_tokens == 2


def test_detect_conflicts_drops_citations_to_clauses_not_shown(monkeypatch):
    candidate = _jurisdiction_clause("j1", "Minimum footing depth 3 feet.")
    response = _fake_response({
        "conflicts": [
            {"severity": "low", "explanation": "Hallucinated citation.", "cited_jurisdiction_clause_ids": ["not-shown"]}
        ]
    })
    monkeypatch.setattr(llm_groq, "_get_client", lambda: _FakeClient(response))

    results, record = llm_groq.detect_conflicts(_clause("Footing depth shall be 5 feet."), [candidate])

    assert results == []  # sole citation wasn't in the shown set, conflict dropped entirely
    assert record is not None  # the call still happened and should be audited


def test_detect_conflicts_no_candidates_skips_api_call():
    # No monkeypatch - if this tried to call the real client it would raise
    # (no API key configured in the test environment).
    results, record = llm_groq.detect_conflicts(_clause("Footing depth shall be 5 feet."), [])
    assert results == []
    assert record is None


def test_detect_conflicts_no_tool_call_returns_empty(monkeypatch):
    candidate = _jurisdiction_clause("j1", "Minimum footing depth 3 feet.")
    message = SimpleNamespace(tool_calls=None, content="no conflicts found")
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=_fake_usage())
    monkeypatch.setattr(llm_groq, "_get_client", lambda: _FakeClient(response))

    results, record = llm_groq.detect_conflicts(_clause("Footing depth shall be 5 feet."), [candidate])

    assert results == []
    assert record is not None  # a real (billed) call was made and should be audited


def test_detect_cross_discipline_conflicts_parses_and_returns_result(monkeypatch):
    candidate = _clause("Ductwork shall maintain a minimum clearance of 9 feet 6 inches.",
                         cid="c-mech", discipline=Discipline.MECHANICAL)
    response = _fake_response({
        "conflicts": [
            {"severity": "high", "explanation": "Clearance mismatch.", "cited_clause_ids": ["c-mech"]}
        ]
    })
    monkeypatch.setattr(llm_groq, "_get_client", lambda: _FakeClient(response))

    structural = _clause("Beam provides a minimum clear height of 9 feet 0 inches.",
                          cid="c-struct", discipline=Discipline.STRUCTURAL)
    results, record = llm_groq.detect_cross_discipline_conflicts(structural, [candidate])

    assert len(results) == 1
    assert results[0].cited_candidate_ids == ["c-mech"]
    assert record is not None


def test_detect_cross_discipline_conflicts_no_candidates_skips_api_call():
    clause = _clause("Beam provides a minimum clear height of 9 feet 0 inches.")
    results, record = llm_groq.detect_cross_discipline_conflicts(clause, [])
    assert results == []
    assert record is None
