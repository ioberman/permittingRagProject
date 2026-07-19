from app.llm_mock import detect_conflicts
from app.models import Clause, JurisdictionClause


def _clause(text, label="4.1"):
    return Clause(id="c1", document_series_id="ds1", clause_label=label, text=text,
                  content_hash="h", extraction_method="pdf_text", location={})


def _jurisdiction_clause(text, label="1607.1", jid="j1"):
    return JurisdictionClause(id=jid, jurisdiction_document_id="jd1", clause_label=label,
                               text=text, content_hash="h2", extraction_method="pdf_text", location={})


def test_flags_when_numbers_differ():
    clause = _clause("Footing depth shall be 5 feet minimum.")
    candidate = _jurisdiction_clause("Minimum footing depth shall be 3 feet.")

    results = detect_conflicts(clause, [candidate])

    assert len(results) == 1
    assert results[0].is_simulated is True
    assert results[0].cited_jurisdiction_clause_ids == [candidate.id]
    assert "SIMULATED" in results[0].explanation


def test_no_flag_when_numbers_match():
    clause = _clause("Footing depth shall be 3 feet minimum.")
    candidate = _jurisdiction_clause("Minimum footing depth shall be 3 feet.")

    assert detect_conflicts(clause, [candidate]) == []


def test_no_flag_when_no_candidates():
    clause = _clause("Footing depth shall be 5 feet minimum.")
    assert detect_conflicts(clause, []) == []


def test_no_flag_when_neither_text_has_numbers():
    clause = _clause("Footings shall bear on undisturbed soil.")
    candidate = _jurisdiction_clause("Footings shall bear on competent bearing material.")
    assert detect_conflicts(clause, [candidate]) == []
