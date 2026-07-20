from app.llm_mock import detect_conflicts, detect_cross_discipline_conflicts
from app.models import Clause, JurisdictionClause


def _clause(text, label="4.1", cid="c1"):
    return Clause(id=cid, document_series_id="ds1", clause_label=label, text=text,
                  content_hash="h", extraction_method="pdf_text", location={})


def _jurisdiction_clause(text, label="1607.1", jid="j1"):
    return JurisdictionClause(id=jid, jurisdiction_document_id="jd1", clause_label=label,
                               text=text, content_hash="h2", extraction_method="pdf_text", location={})


def test_flags_when_numbers_differ():
    clause = _clause("Footing depth shall be 5 feet minimum.")
    candidate = _jurisdiction_clause("Minimum footing depth shall be 3 feet.")

    results, record = detect_conflicts(clause, [candidate])

    assert len(results) == 1
    assert results[0].is_simulated is True
    assert results[0].cited_candidate_ids == [candidate.id]
    assert "SIMULATED" in results[0].explanation
    assert record is not None
    assert record.model == "mock-keyword-heuristic"


def test_no_flag_when_numbers_match():
    clause = _clause("Footing depth shall be 3 feet minimum.")
    candidate = _jurisdiction_clause("Minimum footing depth shall be 3 feet.")

    results, record = detect_conflicts(clause, [candidate])
    assert results == []
    assert record is not None


def test_no_flag_when_no_candidates():
    clause = _clause("Footing depth shall be 5 feet minimum.")
    results, record = detect_conflicts(clause, [])
    assert results == []
    assert record is None


def test_no_flag_when_neither_text_has_numbers():
    clause = _clause("Footings shall bear on undisturbed soil.")
    candidate = _jurisdiction_clause("Footings shall bear on competent bearing material.")
    results, record = detect_conflicts(clause, [candidate])
    assert results == []
    assert record is not None


def test_label_prefix_is_not_treated_as_a_substantive_number():
    # Real extracted clauses include their own label as the leading token of
    # their text (e.g. "4.1 Footing depth..."). Without excluding it, "4.1"
    # would show up as a bogus "number" and pollute the comparison/explanation.
    clause = _clause("4.1 Footing depth shall be 5 feet minimum.", label="4.1")
    candidate = _jurisdiction_clause("1607.1 Minimum footing depth shall be 3 feet.", label="1607.1")

    results, record = detect_conflicts(clause, [candidate])

    assert len(results) == 1
    assert "4.1" not in results[0].explanation.split("this clause says")[1].split(",")[0]
    assert "this clause says 5" in results[0].explanation
    assert "the code section says 3" in results[0].explanation


def test_cross_discipline_flags_when_numbers_differ():
    clause = _clause("Structural beam provides a minimum clear height of 9 feet 0 inches.", cid="c-struct")
    candidate = _clause("Ductwork shall maintain a minimum clearance of 8 feet 6 inches.", label="1.2", cid="c-mech")

    results, record = detect_cross_discipline_conflicts(clause, [candidate])

    assert len(results) == 1
    assert results[0].is_simulated is True
    assert results[0].cited_candidate_ids == [candidate.id]
    assert record is not None


def test_cross_discipline_no_flag_when_no_candidates():
    clause = _clause("Structural beam provides a minimum clear height of 9 feet 0 inches.")
    results, record = detect_cross_discipline_conflicts(clause, [])
    assert results == []
    assert record is None
