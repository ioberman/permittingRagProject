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

# jurisdiction_id -> (sorted JurisdictionClause ids used, their embeddings).
# JurisdictionClause rows are effectively immutable once ingested (a
# jurisdiction document is only ever added to, never edited in place), so
# re-encoding the same corpus on every single check run was pure waste -
# measured directly against the real Chicago dataset (3,609 clauses): 2.8s
# per call on a fast dev machine, worse on a constrained VM, and this ran
# once per check request regardless of how many project clauses were being
# checked. The cached id list (not just the jurisdiction_id) is what keeps
# this correct if a jurisdiction is ever re-ingested with new content - a
# mismatch just falls through to a fresh encode.
_jurisdiction_corpus_cache: dict[str, tuple[list[str], object]] = {}


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def find_candidate_jurisdiction_clauses(
    session: Session, clauses: list[Clause], jurisdiction_id: str, top_n: int = DEFAULT_TOP_N
) -> dict[str, list[JurisdictionClause]]:
    """Maps each project Clause.id to its top-N most similar JurisdictionClause
    rows for the given jurisdiction - pooling both user-uploaded documents and
    anything app/freshness/jurisdiction_sync.py has fed in from a scheduled
    Municode scrape.

    Ranking prefers whichever *source document is more recently confirmed
    current* on a near-tie (same similarity rounded to 2dp), not "uploaded
    always wins" - an uploaded doc that's gone stale for months shouldn't
    outrank scraped content a check re-confirmed as current today, and a
    fresh upload shouldn't be second-guessed by an older scrape either. A
    clearly more relevant candidate from either source still surfaces
    regardless of recency; this only breaks genuine near-ties.
    """
    jurisdiction_clauses = (
        session.query(JurisdictionClause)
        .join(JurisdictionDocument)
        .filter(JurisdictionDocument.jurisdiction_id == jurisdiction_id)
        .order_by(JurisdictionClause.id)
        .all()
    )
    if not clauses or not jurisdiction_clauses:
        return {}

    clause_ids = [jc.id for jc in jurisdiction_clauses]
    recency = [jc.document.ingested_at for jc in jurisdiction_clauses]
    cached = _jurisdiction_corpus_cache.get(jurisdiction_id)
    model = _get_model()
    if cached is not None and cached[0] == clause_ids:
        corpus_embeddings = cached[1]
    else:
        corpus_embeddings = model.encode([jc.text for jc in jurisdiction_clauses], normalize_embeddings=True)
        _jurisdiction_corpus_cache[jurisdiction_id] = (clause_ids, corpus_embeddings)

    query_embeddings = model.encode([c.text for c in clauses], normalize_embeddings=True)
    similarities = query_embeddings @ corpus_embeddings.T  # cosine similarity (embeddings are normalized)

    result = {}
    for clause, row in zip(clauses, similarities):
        ranked = sorted(range(len(row)), key=lambda i: (round(float(row[i]), 2), recency[i]), reverse=True)
        indices = [i for i in ranked[:top_n] if row[i] > SIMILARITY_THRESHOLD]
        result[clause.id] = [jurisdiction_clauses[i] for i in indices]
    return result


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
