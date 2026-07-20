from app.models import Clause, Discipline, DocumentSeries
from app.retrieval import (
    find_candidate_cross_discipline_clauses,
    find_candidate_cross_discipline_clauses_scored,
    rank_candidates,
)


def test_rank_candidates_finds_the_closest_match():
    query_texts = [
        "Footing depth shall be at least 4 feet below grade per local frost line requirements.",
        "Concrete strength shall be 4000 psi at 28 days.",
    ]
    corpus_texts = [
        "1607.1 Minimum foundation footing depth shall be established based on local frost penetration data.",
        "1905.1 Concrete compressive strength requirements for structural elements.",
        "3001.1 Elevator car dimensions and clearances.",
    ]

    results = rank_candidates(query_texts, corpus_texts, top_n=2)

    assert results[0][0] == 0  # footing query -> footing corpus entry ranked first
    assert results[1][0] == 1  # concrete query -> concrete corpus entry ranked first
    assert 2 not in results[0]  # elevator clause has no overlap, excluded
    assert 2 not in results[1]


def test_rank_candidates_handles_empty_inputs():
    assert rank_candidates([], ["some text"]) == []
    assert rank_candidates(["some text"], []) == [[]]


def _clause(text, discipline, label="1.1"):
    series = DocumentSeries(id=f"series-{discipline.value}", project_id="p1", discipline=discipline,
                             sheet_number=f"{discipline.value}-1", title="Test Sheet")
    clause = Clause(id=f"c-{discipline.value}-{label}", document_series_id=series.id, clause_label=label,
                     text=text, content_hash="h", extraction_method="pdf_text", location={},
                     first_seen_document_id="d1")
    clause.series = series
    return clause


def test_find_candidate_cross_discipline_clauses_excludes_same_discipline():
    structural = _clause("Structural beam B-4 provides a minimum clear height of 9 feet 0 inches "
                          "above finished floor within Corridor 104.", Discipline.STRUCTURAL)
    mechanical = _clause("Ductwork DS-2 within Corridor 104 shall maintain a minimum clearance of "
                          "9 feet 6 inches above finished floor to the bottom of duct insulation.",
                          Discipline.MECHANICAL)
    other_structural = _clause("Structural beam B-9 provides a minimum clear height of 8 feet 0 inches "
                                "above finished floor within Corridor 110.", Discipline.STRUCTURAL, label="1.2")

    result = find_candidate_cross_discipline_clauses([structural, mechanical, other_structural])

    # structural's candidates should include mechanical (different discipline, related topic)
    # but never other_structural (same discipline), regardless of topical similarity.
    candidate_ids = {c.id for c in result[structural.id]}
    assert mechanical.id in candidate_ids
    assert other_structural.id not in candidate_ids


def test_find_candidate_cross_discipline_clauses_single_clause_yields_nothing():
    assert find_candidate_cross_discipline_clauses([_clause("Some note.", Discipline.STRUCTURAL)]) == {}


def test_find_candidate_cross_discipline_clauses_scored_matches_unscored():
    structural = _clause("Structural beam B-4 provides a minimum clear height of 9 feet 0 inches "
                          "above finished floor within Corridor 104.", Discipline.STRUCTURAL)
    mechanical = _clause("Ductwork DS-2 within Corridor 104 shall maintain a minimum clearance of "
                          "9 feet 6 inches above finished floor to the bottom of duct insulation.",
                          Discipline.MECHANICAL)

    unscored = find_candidate_cross_discipline_clauses([structural, mechanical])
    scored = find_candidate_cross_discipline_clauses_scored([structural, mechanical])

    assert [c.id for c in unscored[structural.id]] == [c.id for c, _ in scored[structural.id]]
    score = scored[structural.id][0][1]
    assert isinstance(score, float)
    assert 0.2 < score <= 1.0  # above SIMILARITY_THRESHOLD, a valid cosine similarity
