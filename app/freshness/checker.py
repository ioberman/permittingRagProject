"""Runs one freshness check for a FreshnessSource: fetch -> normalize -> hash
-> diff against the latest snapshot -> persist.

The change-detection hash is computed over each source's *normalized* content
(for ICC_PUBLIC, just the sorted edition years), not raw response bytes. Raw
sitemap XML carries lastmod timestamps and unrelated category URLs that churn
on ICC's own schedule for reasons that have nothing to do with a new code
edition - hashing raw bytes would fire a false "change" on nearly every run.
Raw bytes are still stored (FreshnessSnapshot.raw_content_uri) for audit, so a
human can always go look at exactly what was fetched.
"""

import hashlib

from app.freshness.icc import extract_edition_years, fetch_icc_sitemap
from app.freshness.municode import (
    extract_sections,
    fetch_municode_content,
    normalize_sections,
    parse_source_ref,
)
from app.models import FreshnessChange, FreshnessSnapshot, FreshnessSource, FreshnessSourceKind
from app.storage import LocalFileStorage

_MAX_SECTIONS_IN_SUMMARY = 5


def _fetch_raw(source: FreshnessSource) -> bytes:
    if source.kind == FreshnessSourceKind.ICC_PUBLIC:
        return fetch_icc_sitemap()
    if source.kind == FreshnessSourceKind.MUNICODE:
        product_id, node_id = parse_source_ref(source.source_ref)
        return fetch_municode_content(node_id, product_id)
    raise NotImplementedError(f"no fetcher implemented for source kind {source.kind}")


def _normalize(source: FreshnessSource, raw: bytes) -> str:
    if source.kind == FreshnessSourceKind.ICC_PUBLIC:
        return ",".join(str(year) for year in extract_edition_years(raw))
    if source.kind == FreshnessSourceKind.MUNICODE:
        return normalize_sections(extract_sections(raw))
    raise NotImplementedError(f"no normalizer implemented for source kind {source.kind}")


def _icc_diff_summary(prev_normalized: str, new_normalized: str) -> str:
    prev_years = set(prev_normalized.split(",")) if prev_normalized else set()
    new_years = set(new_normalized.split(","))
    added = sorted(new_years - prev_years)
    removed = sorted(prev_years - new_years)
    parts = []
    if added:
        parts.append(f"new edition year(s) detected: {', '.join(added)}")
    if removed:
        parts.append(f"edition year(s) no longer listed: {', '.join(removed)}")
    return "; ".join(parts) if parts else "sitemap content changed, no edition-year difference detected"


def _municode_diff_summary(prev_normalized: str, new_normalized: str) -> str:
    def by_id(normalized: str) -> dict[str, tuple[str, str]]:
        result = {}
        for line in normalized.split("\n"):
            if not line:
                continue
            doc_id, title, text = line.split("\t", 2)
            result[doc_id] = (title, text)
        return result

    prev_sections, new_sections = by_id(prev_normalized), by_id(new_normalized)
    added = [doc_id for doc_id in new_sections if doc_id not in prev_sections]
    removed = [doc_id for doc_id in prev_sections if doc_id not in new_sections]
    changed = [
        doc_id
        for doc_id in new_sections
        if doc_id in prev_sections and new_sections[doc_id] != prev_sections[doc_id]
    ]

    parts = []
    if changed:
        titles = [new_sections[doc_id][0] for doc_id in changed[:_MAX_SECTIONS_IN_SUMMARY]]
        parts.append(f"section(s) changed: {', '.join(titles)}")
    if added:
        titles = [new_sections[doc_id][0] for doc_id in added[:_MAX_SECTIONS_IN_SUMMARY]]
        parts.append(f"section(s) added: {', '.join(titles)}")
    if removed:
        titles = [prev_sections[doc_id][0] for doc_id in removed[:_MAX_SECTIONS_IN_SUMMARY]]
        parts.append(f"section(s) removed: {', '.join(titles)}")
    return "; ".join(parts) if parts else "content changed, no section-level difference detected"


def _diff_summary(source: FreshnessSource, prev_normalized: str, new_normalized: str) -> str:
    if source.kind == FreshnessSourceKind.ICC_PUBLIC:
        return _icc_diff_summary(prev_normalized, new_normalized)
    if source.kind == FreshnessSourceKind.MUNICODE:
        return _municode_diff_summary(prev_normalized, new_normalized)
    raise NotImplementedError(f"no summarizer implemented for source kind {source.kind}")


def _raw_filename(source: FreshnessSource) -> str:
    extension = "json" if source.kind == FreshnessSourceKind.MUNICODE else "xml"
    return f"{source.id}.{extension}"


def run_check(source: FreshnessSource, session, storage: LocalFileStorage | None = None) -> FreshnessSnapshot:
    """Fetches fresh content for `source`, always records a new snapshot, and
    writes a FreshnessChange row only if content differs from the most recent
    prior snapshot. Returns the newly created snapshot."""
    storage = storage or LocalFileStorage()

    raw = _fetch_raw(source)
    normalized = _normalize(source, raw)
    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    previous = (
        session.query(FreshnessSnapshot)
        .filter_by(source_id=source.id)
        .order_by(FreshnessSnapshot.fetched_at.desc())
        .first()
    )

    raw_content_uri, _ = storage.save(raw, filename=_raw_filename(source))
    snapshot = FreshnessSnapshot(
        source_id=source.id,
        content_hash=content_hash,
        raw_content_uri=raw_content_uri,
    )
    session.add(snapshot)
    session.flush()  # assigns snapshot.id, needed below before commit

    if previous is not None and previous.content_hash != content_hash:
        prev_raw = storage.load(previous.raw_content_uri)
        prev_normalized = _normalize(source, prev_raw)
        session.add(
            FreshnessChange(
                source_id=source.id,
                prev_snapshot_id=previous.id,
                new_snapshot_id=snapshot.id,
                summary=_diff_summary(source, prev_normalized, normalized),
            )
        )

    session.commit()
    return snapshot
