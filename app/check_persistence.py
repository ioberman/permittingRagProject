"""Shared persistence logic for both check families (jurisdiction-compliance
and cross-discipline coordination), plus the "current state of a project"
queries both checks and the UI depend on.

Core idea: Clause rows are immutable and content-hash deduped (see
app/models.py) - a clause's content can never change under you, "a clause
changed" always means a brand new Clause row exists. That's what makes
incremental re-check correct and cheap: a clause only ever needs reasoning
once per check_type, ever, regardless of which submission introduced it or
which submission is "current" now. run_check skips any clause that already
has an LLMCall of that check_type from ANY of the project's submissions,
unless force=True.

Because of that, "current flags for a project" is not "flags on the latest
submission_id" - it's every Flag whose clause_id is still part of the
project's current clause set (see current_project_clauses), regardless of
which submission actually ran the check that produced it. A flag on a
clause that's since been superseded by a revision naturally drops out of
"current" without ever being deleted - the row (and its LLMCall) stays in
the database as audit history.
"""

from typing import Callable

from sqlalchemy.orm import Session

from app.models import (
    CheckType,
    Clause,
    Document,
    DocumentClause,
    DocumentSeries,
    Flag,
    FlagCitation,
    FlagSeverity,
    LLMCall,
    Submission,
)


def _documents_as_of(session: Session, project_id: str, max_sequence_number: int | None = None) -> list[Document]:
    """For each sheet (DocumentSeries) in the project, its most recent
    Document version at or before max_sequence_number (or overall, if None).
    The building block both "current state" and "state as of revision N"
    (used by diffing) are defined in terms of."""
    query = (
        session.query(Document)
        .join(DocumentSeries, Document.document_series_id == DocumentSeries.id)
        .join(Submission, Document.submission_id == Submission.id)
        .filter(DocumentSeries.project_id == project_id)
    )
    if max_sequence_number is not None:
        query = query.filter(Submission.sequence_number <= max_sequence_number)
    all_docs = query.order_by(Submission.sequence_number.asc()).all()

    latest_by_series: dict[str, Document] = {}
    for doc in all_docs:
        latest_by_series[doc.document_series_id] = doc  # later (higher sequence_number) overwrites earlier
    return list(latest_by_series.values())


def current_documents_for_project(session: Session, project_id: str) -> list[Document]:
    """Each sheet's most recent Document version, across ALL submissions -
    not just the latest one. Starting a new revision (Submission) is a
    project-wide checkpoint, but it's not a snapshot: only sheets actually
    re-uploaded get a Document row in it. Without this, "documents tied to
    submission X" would silently drop every sheet that wasn't re-uploaded in
    the same batch as whichever sheet triggered the new revision."""
    return _documents_as_of(session, project_id)


def _clause_ids_for_document(session: Session, document_id: str) -> set[str]:
    return {row[0] for row in session.query(DocumentClause.clause_id).filter_by(document_id=document_id).all()}


def current_project_clauses(session: Session, project_id: str) -> list[Clause]:
    """Every clause belonging to the project's current sheet set - what a
    check should reason over, regardless of which submission each sheet's
    latest version happens to have been uploaded into."""
    document_ids = [d.id for d in current_documents_for_project(session, project_id)]
    if not document_ids:
        return []
    return (
        session.query(Clause)
        .join(DocumentClause, DocumentClause.clause_id == Clause.id)
        .filter(DocumentClause.document_id.in_(document_ids))
        .distinct()
        .all()
    )


def current_flags_for_project(session: Session, project_id: str, check_type: CheckType | None = None) -> list[Flag]:
    """Flags about clauses that are still part of the project's current
    state. A clause superseded by a later revision (replaced by a new Clause
    row) simply stops appearing here once it's no longer "current" - its old
    Flag/LLMCall rows aren't deleted, just no longer shown as live."""
    current_clause_ids = {c.id for c in current_project_clauses(session, project_id)}
    if not current_clause_ids:
        return []
    query = (
        session.query(Flag)
        .join(Submission, Flag.submission_id == Submission.id)
        .filter(Submission.project_id == project_id, Flag.clause_id.in_(current_clause_ids))
    )
    if check_type is not None:
        query = query.filter(Flag.check_type == check_type)
    return query.all()


def diff_between_submissions(session: Session, project_id: str, from_seq: int, to_seq: int) -> list[dict]:
    """Clause-level diff for every sheet in the project between two points
    in its revision history (by Submission.sequence_number). Possible
    without any text-diffing because Clause rows are content-hash deduped -
    "added"/"removed" is just a set difference over DocumentClause links.
    Sheets with no change between the two points are omitted entirely."""
    from_docs = {d.document_series_id: d for d in _documents_as_of(session, project_id, from_seq)}
    to_docs = {d.document_series_id: d for d in _documents_as_of(session, project_id, to_seq)}

    results = []
    for series_id in set(from_docs) | set(to_docs):
        from_doc = from_docs.get(series_id)
        to_doc = to_docs.get(series_id)
        from_clause_ids = _clause_ids_for_document(session, from_doc.id) if from_doc else set()
        to_clause_ids = _clause_ids_for_document(session, to_doc.id) if to_doc else set()

        added_ids = to_clause_ids - from_clause_ids
        removed_ids = from_clause_ids - to_clause_ids
        if not added_ids and not removed_ids and from_doc is not None and to_doc is not None:
            continue  # sheet unchanged between these two points

        results.append({
            "series": session.get(DocumentSeries, series_id),
            "is_new_sheet": from_doc is None,
            "is_removed_sheet": to_doc is None,
            "added": session.query(Clause).filter(Clause.id.in_(added_ids)).all() if added_ids else [],
            "removed": session.query(Clause).filter(Clause.id.in_(removed_ids)).all() if removed_ids else [],
            "unchanged_count": len(from_clause_ids & to_clause_ids),
        })
    return results


def _already_checked_clause_ids(session: Session, project_id: str, check_type: CheckType) -> set[str]:
    """Clause ids that already have an LLMCall of this check_type from any
    of the project's submissions - since Clause rows are immutable, a clause
    only ever needs reasoning once per check_type, ever."""
    rows = (
        session.query(LLMCall.clause_id)
        .join(Submission, LLMCall.submission_id == Submission.id)
        .filter(Submission.project_id == project_id, LLMCall.check_type == check_type)
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def _clear_project_check_results(session: Session, project_id: str, check_type: CheckType) -> None:
    """Used only for an explicit force re-check - clears this check_type's
    Flag/FlagCitation/LLMCall rows across the WHOLE project's history, not
    just one submission, since incremental checks may have spread them
    across several submissions over time."""
    submission_ids = [row[0] for row in session.query(Submission.id).filter_by(project_id=project_id).all()]
    flag_ids = session.query(Flag.id).filter(Flag.submission_id.in_(submission_ids), Flag.check_type == check_type)
    session.query(FlagCitation).filter(FlagCitation.flag_id.in_(flag_ids)).delete(synchronize_session=False)
    session.query(Flag).filter(Flag.submission_id.in_(submission_ids), Flag.check_type == check_type).delete(synchronize_session=False)
    session.query(LLMCall).filter(LLMCall.submission_id.in_(submission_ids), LLMCall.check_type == check_type).delete(synchronize_session=False)


def run_check(
    session: Session,
    submission: Submission,
    engine_name: str,
    check_type: CheckType,
    clauses: list[Clause],
    candidates_by_clause: dict[str, list],
    detect_fn: Callable,
    citation_field: str,  # "jurisdiction_clause_id" or "clause_id"
    force: bool = False,
) -> list[Flag]:
    """Runs detect_fn only on clauses this check_type hasn't already reasoned
    about (incremental) - or on everything, if force=True clears prior
    results project-wide first and starts clean. New LLMCall/Flag rows
    attach to `submission` (the current checkpoint); untouched clauses keep
    whichever earlier submission's rows they already had. Returns the
    project's full current set of flags for this check_type, not just the
    ones created in this call - that's what callers actually want to know."""
    project_id = submission.project_id

    if force:
        _clear_project_check_results(session, project_id, check_type)
        already_checked = set()
    else:
        already_checked = _already_checked_clause_ids(session, project_id, check_type)

    for clause in clauses:
        if clause.id in already_checked:
            continue

        candidates = candidates_by_clause.get(clause.id, [])
        results, call_record = detect_fn(clause, candidates)

        llm_call = None
        if call_record is not None:
            llm_call = LLMCall(
                submission_id=submission.id,
                clause_id=clause.id,
                check_type=check_type,
                engine=engine_name,
                model=call_record.model,
                prompt=call_record.prompt,
                raw_response=call_record.raw_response,
                input_tokens=call_record.input_tokens,
                output_tokens=call_record.output_tokens,
                latency_ms=call_record.latency_ms,
            )
            session.add(llm_call)
            session.flush()

        for result in results:
            flag = Flag(
                submission_id=submission.id,
                clause_id=clause.id,
                llm_call_id=llm_call.id,
                check_type=check_type,
                severity=FlagSeverity(result.severity),
                explanation=result.explanation,
                model=result.model,
                is_simulated=result.is_simulated,
            )
            session.add(flag)
            session.flush()
            for candidate_id in result.cited_candidate_ids:
                session.add(FlagCitation(flag_id=flag.id, **{citation_field: candidate_id}))

    session.flush()
    return current_flags_for_project(session, project_id, check_type)
