"""Read-only freshness status for the jurisdictions dashboard - pulls together
what the scheduled ICC/Municode checks (app/freshness/scheduler.py) have found:
a global ICC banner (not jurisdiction-specific, see FreshnessSource's
docstring), a one-row-per-jurisdiction summary, and a per-jurisdiction detail
view with full change history.
"""

import re
from datetime import datetime

from app.freshness.icc import extract_edition_years
from app.models import (
    DocType,
    FreshnessChange,
    FreshnessSnapshot,
    FreshnessSource,
    FreshnessSourceKind,
    Jurisdiction,
    JurisdictionClause,
    JurisdictionDocument,
)
from app.storage import LocalFileStorage


# Matches the map SVG's own per-state class names (app/templates/_us_map.html).
_VALID_STATE_ABBRS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo",
    "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa",
    "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
}
_TRAILING_STATE_RE = re.compile(r",\s*([A-Za-z]{2})$")


def state_abbr_from_name(jurisdiction_name: str) -> str | None:
    """Every jurisdiction in this app is named "<place>, <ST>" by convention
    (not schema-enforced - a free-text field), so this is best-effort: a name
    that doesn't end in a real two-letter state code just doesn't show up on
    the map, but still shows up in the table below it."""
    match = _TRAILING_STATE_RE.search(jurisdiction_name)
    if not match:
        return None
    abbr = match.group(1).lower()
    return abbr if abbr in _VALID_STATE_ABBRS else None


def _latest_snapshot(session, source_id: str) -> FreshnessSnapshot | None:
    return (
        session.query(FreshnessSnapshot)
        .filter_by(source_id=source_id)
        .order_by(FreshnessSnapshot.fetched_at.desc())
        .first()
    )


def _latest_change(session, source_id: str) -> FreshnessChange | None:
    return (
        session.query(FreshnessChange)
        .filter_by(source_id=source_id)
        .order_by(FreshnessChange.detected_at.desc())
        .first()
    )


def _source_status(session, source: FreshnessSource) -> dict:
    latest_snapshot = _latest_snapshot(session, source.id)
    latest_change = _latest_change(session, source.id)
    changed_on_last_check = (
        latest_change is not None
        and latest_snapshot is not None
        and latest_change.new_snapshot_id == latest_snapshot.id
    )
    return {
        "id": source.id,
        "label": source.label,
        "last_checked_at": latest_snapshot.fetched_at if latest_snapshot else None,
        "changed_on_last_check": changed_on_last_check,
        "last_change_at": latest_change.detected_at if latest_change else None,
        "last_change_summary": latest_change.summary if latest_change else None,
    }


def icc_banner_status(session, storage: LocalFileStorage | None = None) -> dict | None:
    """None if the ICC source hasn't been seeded yet (shouldn't happen once
    app/freshness/seed.py has run, but the template should not assume)."""
    source = session.query(FreshnessSource).filter_by(kind=FreshnessSourceKind.ICC_PUBLIC).first()
    if source is None:
        return None

    storage = storage or LocalFileStorage()
    latest_snapshot = _latest_snapshot(session, source.id)
    latest_edition_year = None
    if latest_snapshot is not None:
        years = extract_edition_years(storage.load(latest_snapshot.raw_content_uri))
        latest_edition_year = max(years) if years else None

    return {
        "last_checked_at": latest_snapshot.fetched_at.strftime("%Y-%m-%d %H:%M") if latest_snapshot else "never",
        "latest_edition_year": latest_edition_year,
    }


def jurisdiction_summary_rows(session) -> list[dict]:
    """One row per jurisdiction for the /jurisdictions index - uploaded_count
    excludes MUNICODE_SCRAPE documents, since those aren't something a human
    uploaded (see app/freshness/jurisdiction_sync.py); clause_count is the
    total across *every* document, uploaded or scraped. Both are shown
    together so a jurisdiction with real live-monitored content (uploaded_count
    0, clause_count >0, e.g. Hartford) doesn't read as empty next to one that
    genuinely has nothing loaded yet. live_status is the most recently checked
    FreshnessSource for that jurisdiction, if any - a jurisdiction can have
    zero (no live monitoring set up for it yet)."""
    rows = []
    for jurisdiction in session.query(Jurisdiction).order_by(Jurisdiction.name).all():
        uploaded_count = (
            session.query(JurisdictionDocument)
            .filter(JurisdictionDocument.jurisdiction_id == jurisdiction.id)
            .filter(JurisdictionDocument.doc_type != DocType.MUNICODE_SCRAPE)
            .count()
        )
        clause_count = (
            session.query(JurisdictionClause)
            .join(JurisdictionDocument)
            .filter(JurisdictionDocument.jurisdiction_id == jurisdiction.id)
            .count()
        )
        sources = session.query(FreshnessSource).filter_by(jurisdiction_id=jurisdiction.id).all()
        statuses = [_source_status(session, s) for s in sources]
        live_status = max(statuses, key=lambda s: s["last_checked_at"] or datetime.min) if statuses else None

        rows.append(
            {
                "id": jurisdiction.id,
                "name": jurisdiction.name,
                "state_abbr": state_abbr_from_name(jurisdiction.name),
                "uploaded_count": uploaded_count,
                "clause_count": clause_count,
                "live_status": live_status,
            }
        )
    return rows


def jurisdiction_detail(session, jurisdiction_id: str) -> dict:
    """Full status for one jurisdiction's page: every configured
    FreshnessSource with its recent change history, not just the latest."""
    sources = session.query(FreshnessSource).filter_by(jurisdiction_id=jurisdiction_id).all()
    source_details = []
    for source in sources:
        status = _source_status(session, source)
        history = (
            session.query(FreshnessChange)
            .filter_by(source_id=source.id)
            .order_by(FreshnessChange.detected_at.desc())
            .limit(10)
            .all()
        )
        status["history"] = [{"detected_at": c.detected_at, "summary": c.summary} for c in history]
        source_details.append(status)
    return {"sources": source_details}
