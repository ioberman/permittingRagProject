"""Narrows candidate clause pairs before the LLM reasons over them.

Per CLAUDE.md: retrieval and generation are separate steps - this avoids
comparing every project clause against every jurisdiction clause (O(n*m),
wastes tokens). Uses a local sentence-embedding model (no API key, no cost)
rather than TF-IDF: real semantic similarity instead of keyword overlap,
while keeping the "runnable without credentials" property the rest of this
project has (see app/llm_mock.py).

SIMILARITY_THRESHOLD is calibrated empirically, not arbitrary: unrelated
clause pairs (different topic entirely) score ~0.13-0.15 cosine similarity
with this model; genuinely related pairs score 0.3+. 0.2 sits in the gap.
"""

from sentence_transformers import SentenceTransformer
from sqlalchemy.orm import Session

from app.models import Clause, JurisdictionClause, JurisdictionDocument

DEFAULT_TOP_N = 5
SIMILARITY_THRESHOLD = 0.2
MODEL_NAME = "all-MiniLM-L6-v2"

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def rank_candidates(query_texts: list[str], corpus_texts: list[str], top_n: int = DEFAULT_TOP_N) -> list[list[int]]:
    """For each query text, returns indices into corpus_texts of the top_n most
    similar entries by embedding cosine similarity, descending. Entries below
    SIMILARITY_THRESHOLD are excluded (an empty list means nothing worth
    showing the LLM)."""
    if not query_texts or not corpus_texts:
        return [[] for _ in query_texts]

    model = _get_model()
    corpus_embeddings = model.encode(corpus_texts, normalize_embeddings=True)
    query_embeddings = model.encode(query_texts, normalize_embeddings=True)
    similarities = query_embeddings @ corpus_embeddings.T  # cosine similarity (embeddings are normalized)

    results = []
    for row in similarities:
        ranked = sorted(range(len(row)), key=lambda i: row[i], reverse=True)
        results.append([i for i in ranked[:top_n] if row[i] > SIMILARITY_THRESHOLD])
    return results


def find_candidate_jurisdiction_clauses(
    session: Session, clauses: list[Clause], jurisdiction_id: str, top_n: int = DEFAULT_TOP_N
) -> dict[str, list[JurisdictionClause]]:
    """Maps each project Clause.id to its top-N most similar JurisdictionClause
    rows for the given jurisdiction."""
    jurisdiction_clauses = (
        session.query(JurisdictionClause)
        .join(JurisdictionDocument)
        .filter(JurisdictionDocument.jurisdiction_id == jurisdiction_id)
        .all()
    )
    if not clauses or not jurisdiction_clauses:
        return {}

    query_texts = [c.text for c in clauses]
    corpus_texts = [jc.text for jc in jurisdiction_clauses]
    ranked_indices = rank_candidates(query_texts, corpus_texts, top_n)

    return {
        clause.id: [jurisdiction_clauses[i] for i in indices]
        for clause, indices in zip(clauses, ranked_indices)
    }


def find_candidate_cross_discipline_clauses_scored(
    clauses: list[Clause], top_n: int = DEFAULT_TOP_N
) -> dict[str, list[tuple[Clause, float]]]:
    """Same matching as find_candidate_cross_discipline_clauses, but also
    returns the cosine similarity score for each candidate - useful for
    displaying retrieval quality on its own (e.g. a no-LLM preview view),
    where the LLM-facing function's returned Clause objects alone don't carry
    "why did retrieval think these were related" for a human to check."""
    if len(clauses) < 2:
        return {}

    model = _get_model()
    texts = [c.text for c in clauses]
    disciplines = [c.series.discipline for c in clauses]
    embeddings = model.encode(texts, normalize_embeddings=True)
    similarities = embeddings @ embeddings.T

    result: dict[str, list[tuple[Clause, float]]] = {}
    for i, clause in enumerate(clauses):
        row = similarities[i]
        ranked = sorted(range(len(clauses)), key=lambda j: row[j], reverse=True)
        result[clause.id] = [
            (clauses[j], float(row[j])) for j in ranked
            if j != i and disciplines[j] != disciplines[i] and row[j] > SIMILARITY_THRESHOLD
        ][:top_n]
    return result


def find_candidate_cross_discipline_clauses(
    clauses: list[Clause], top_n: int = DEFAULT_TOP_N
) -> dict[str, list[Clause]]:
    """Maps each Clause.id to its top-N most similar Clause rows from OTHER
    disciplines within the same list (typically every clause in one
    submission). Same-discipline matches are excluded outright - a
    structural clause matching another structural clause isn't a
    coordination conflict, it's just the same team's own document, a
    different problem entirely."""
    scored = find_candidate_cross_discipline_clauses_scored(clauses, top_n)
    return {clause_id: [c for c, _ in pairs] for clause_id, pairs in scored.items()}
