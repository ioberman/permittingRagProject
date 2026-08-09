"""Background scheduler for freshness checks - runs inside the Flask app
process itself (per project decision: integrated module, not a standalone
service), the same way app/web.py already backgrounds embedding-model warm-up
and demo seeding via a daemon thread.

Polls on one short, fixed tick (_POLL_INTERVAL_SECONDS) rather than sleeping
for each source's own check_interval_seconds, so sources with different
cadences (ICC daily, Municode every few hours once added) share a single loop
without needing a thread per source.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from app.db import get_session
from app.freshness.checker import run_check
from app.freshness.jurisdiction_sync import sync_scraped_clauses
from app.models import FreshnessSnapshot, FreshnessSource, FreshnessSourceKind

_POLL_INTERVAL_SECONDS = 60


def _is_due(source: FreshnessSource, latest_snapshot: FreshnessSnapshot | None) -> bool:
    if latest_snapshot is None:
        return True
    # SQLite drops tzinfo on the round trip through storage - fetched_at reads
    # back naive even though _now() wrote it as UTC - so compare naive-to-naive
    # rather than against an aware datetime.now(timezone.utc) directly.
    now_naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    elapsed = now_naive_utc - latest_snapshot.fetched_at
    return elapsed >= timedelta(seconds=source.check_interval_seconds)


def _run_due_checks() -> None:
    session = get_session()
    try:
        for source in session.query(FreshnessSource).all():
            latest = (
                session.query(FreshnessSnapshot)
                .filter_by(source_id=source.id)
                .order_by(FreshnessSnapshot.fetched_at.desc())
                .first()
            )
            if not _is_due(source, latest):
                continue
            try:
                snapshot = run_check(source, session)
                print(f"[freshness] checked {source.label}: snapshot {snapshot.id}", flush=True)
                if source.kind == FreshnessSourceKind.MUNICODE:
                    clause_count = sync_scraped_clauses(session, source, snapshot)
                    print(f"[freshness] synced {clause_count} clause(s) for {source.label}", flush=True)
            except Exception as e:
                # One source's fetch breaking (unofficial/scraped sources are
                # expected to be fragile - see app/freshness/icc.py) must never
                # take down the whole scheduler loop or the app process.
                print(f"[freshness] check failed for {source.label!r}: {e}", flush=True)
    finally:
        session.close()


def _loop() -> None:
    while True:
        try:
            _run_due_checks()
        except Exception as e:
            print(f"[freshness] scheduler tick failed: {e}", flush=True)
        time.sleep(_POLL_INTERVAL_SECONDS)


def start_scheduler() -> None:
    threading.Thread(target=_loop, daemon=True).start()
