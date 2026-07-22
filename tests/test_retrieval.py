from app.models import (
    Clause,
    Discipline,
    DocumentSeries,
    DocType,
    ExtractionMethod,
    JurisdictionClause,
    JurisdictionDocument,
)
from app.retrieval import (
    _jurisdiction_corpus_cache,
    find_candidate_cross_discipline_clauses,
    find_candidate_cross_discipline_clauses_scored,
    find_candidate_jurisdiction_clauses,
)


def _clause(text, discipline, label="1.1"):
    series = DocumentSeries(id=f"series-{discipline.value}", project_id="p1", discipline=discipline,
                             sheet_number=f"{discipline.value}-1", title="Test Sheet")
    clause = Clause(id=f"c-{discipline.value}-{label}", document_series_id=series.id, clause_label=label,
                     text=text, content_hash="h", extraction_method="pdf_text", location={},
                     first_seen_document_id="d1")
    clause.series = series
    return clause


def _jurisdiction_clause(session, jurisdiction, label, text):
    document = session.query(JurisdictionDocument).filter_by(jurisdiction_id=jurisdiction.id).first()
    if document is None:
        document = JurisdictionDocument(
            jurisdiction_id=jurisdiction.id, title="Test Code", doc_type=DocType.PDF_2D,
            file_uri="x", file_hash="x",
        )
        session.add(document)
        session.flush()
    clause = JurisdictionClause(
        jurisdiction_document_id=document.id, clause_label=label, text=text,
        content_hash=f"h-{label}", extraction_method=ExtractionMethod.PDF_TEXT, location={},
    )
    session.add(clause)
    session.flush()
    return clause


def test_find_candidate_jurisdiction_clauses_finds_relevant_match(session, jurisdiction):
    footing = _jurisdiction_clause(
        session, jurisdiction, "1607.1",
        "Minimum foundation footing depth shall be established based on local frost penetration data.",
    )
    _jurisdiction_clause(session, jurisdiction, "3001.1", "Elevator car dimensions and clearances.")
    project_clause = _clause(
        "Footing depth shall be at least 4 feet below grade per local frost line requirements.",
        Discipline.STRUCTURAL,
    )

    result = find_candidate_jurisdiction_clauses(session, [project_clause], jurisdiction.id)

    candidate_ids = {c.id for c in result[project_clause.id]}
    assert footing.id in candidate_ids


def test_find_candidate_jurisdiction_clauses_no_jurisdiction_docs_yields_nothing(session, jurisdiction):
    project_clause = _clause("Some project note.", Discipline.STRUCTURAL)
    assert find_candidate_jurisdiction_clauses(session, [project_clause], jurisdiction.id) == {}


def test_find_candidate_jurisdiction_clauses_no_project_clauses_yields_nothing(session, jurisdiction):
    _jurisdiction_clause(session, jurisdiction, "1.1", "Some code text.")
    assert find_candidate_jurisdiction_clauses(session, [], jurisdiction.id) == {}


def test_find_candidate_jurisdiction_clauses_caches_corpus_embeddings(session, jurisdiction, monkeypatch):
    _jurisdiction_corpus_cache.clear()
    _jurisdiction_clause(session, jurisdiction, "1607.1", "Minimum foundation footing depth requirements.")
    project_clause = _clause("Footing depth shall be at least 4 feet below grade.", Discipline.STRUCTURAL)

    import app.retrieval as retrieval_module

    real_get_model = retrieval_module._get_model
    encode_calls = []
    model = real_get_model()
    original_encode = model.encode

    def counting_encode(texts, **kwargs):
        encode_calls.append(list(texts))
        return original_encode(texts, **kwargs)

    monkeypatch.setattr(model, "encode", counting_encode)

    find_candidate_jurisdiction_clauses(session, [project_clause], jurisdiction.id)
    # First call: one encode for the jurisdiction corpus, one for the query clause.
    assert len(encode_calls) == 2

    encode_calls.clear()
    find_candidate_jurisdiction_clauses(session, [project_clause], jurisdiction.id)
    # Second call against the same, unchanged jurisdiction: corpus is cached,
    # so only the query clause gets encoded this time - this is the whole
    # point of the cache (see app/retrieval.py's docstring on it).
    assert len(encode_calls) == 1


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
