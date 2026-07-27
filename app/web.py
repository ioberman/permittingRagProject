# Very simple web UI for ingesting documents, for local use only. Not intended to be a full-featured document management system.

"""Local web UI, organized project-first: the home page lists projects, and
everything about one project (its sheets, gaps, checks, revision history)
lives on that project's own page instead of a flat page of dropdown-driven
forms. Field count on the add-sheet form is kept to what a reviewer actually
knows off the top of their head - sheet number, title, discipline, file.
Revision and document type are derived rather than asked:
  - revision: uploads default to the project's latest revision, so a batch of
    sheets naturally groups together. Starting a new revision is a separate,
    explicit action, not a per-upload field.
  - document type: inferred from the file extension.

Templates live in app/templates/ (Jinja files, not Python string constants)
and share app/static/style.css - a normal Flask app layout, deployable as-is
to any real Python host (Render, Railway, Fly, ...). GitHub Pages is NOT an
option for this app specifically: it only serves static files and can't run
the Flask process, hit the database, or call an LLM API.
"""

import csv
import io
import os
import re
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import Flask, g, jsonify, redirect, render_template, request, Response

# stdout is block-buffered, not line-buffered, once it's piped/redirected
# rather than an interactive terminal - exactly gunicorn's case on Render.
# Confirmed directly: a background thread's print() calls didn't show up in
# a redirected log file even 8 seconds later, while gunicorn's own
# logging-module output flushed immediately. Reconfiguring here (rather than
# adding flush=True to every print call, in this file or any script it
# imports) fixes visibility for all of them at once, including the seed
# scripts' own per-file progress lines.
try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass  # Python <3.7 fallback; not a real concern for this project's target versions

from app.check_persistence import (
    current_documents_for_project,
    current_flags_for_project,
    current_project_clauses,
    diff_between_submissions,
    document_graph_data,
)
from app.clause_extraction import (
    SHEET_NUMBER_TOKEN,
    extract_and_store_clauses,
    extract_and_store_jurisdiction_clauses,
    sniff_sheet_number,
)
from app.conflict_detection import check_submission_for_conflicts
from app.cross_discipline_detection import check_submission_for_cross_discipline_conflicts
from app.db import get_session as _get_session
from app.db import init_db
from app.ingest import (
    DEFAULT_SUBMITTED_BY,
    delete_project_cascade,
    find_series,
    get_latest_or_create_submission,
    get_or_create_jurisdiction,
    get_or_create_project,
    create_submission,
    ingest_document,
    ingest_jurisdiction_document,
    infer_doc_type,
    jurisdictions_with_documentation,
    storage,
)
from app.models import (
    CheckType,
    Clause,
    Discipline,
    Document,
    DocumentClause,
    DocumentSeries,
    Flag,
    FlagStatus,
    Jurisdiction,
    JurisdictionClause,
    JurisdictionDocument,
    LLMCall,
    Project,
    Submission,
)
from app.retrieval import find_candidate_cross_discipline_clauses_scored

app = Flask(__name__)
# Keep the application limit aligned with deploy/oracle/nginx-upload-limits.conf.
# Nginx normally rejects oversized requests first in production, while this
# protects local development and deployments that expose gunicorn directly.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
# Off by default outside debug mode, which means a long-running `flask run`
# process silently keeps serving whatever a template looked like on its
# first render - editing the .html file on disk has no effect until the
# process restarts. Cheap to force on (an mtime check per render) and worth
# it to not chase a "bug" that's actually just a stale server.
app.config["TEMPLATES_AUTO_RELOAD"] = True
init_db()

# Tracks checks running in a background thread (see check()/check_cross_discipline()
# below) so the project page can show "running" and poll instead of a request
# blocking until a real, possibly 1-2 minute, sequence of LLM calls finishes -
# that's what was hitting nginx/gunicorn's timeouts on the live deployment.
# In-memory only, not DB-backed: single-process by design (see
# AUTO_SEED_ON_START's own reasoning on why more workers need care), so this
# doesn't need to survive a restart - a check interrupted by one was already
# going to need re-running anyway.
_running_checks_lock = threading.Lock()
_running_checks: set[tuple[str, str]] = set()  # (project_id, check_type value)
_check_errors: dict[tuple[str, str], str] = {}


def _is_check_running(project_id: str, check_type_value: str) -> bool:
    with _running_checks_lock:
        return (project_id, check_type_value) in _running_checks


def _start_check_tracking(project_id: str, check_type_value: str) -> None:
    with _running_checks_lock:
        _running_checks.add((project_id, check_type_value))
        _check_errors.pop((project_id, check_type_value), None)


def _finish_check_tracking(project_id: str, check_type_value: str, error: str | None = None) -> None:
    with _running_checks_lock:
        _running_checks.discard((project_id, check_type_value))
        if error:
            _check_errors[(project_id, check_type_value)] = error


def _describe_check_error(e: Exception, label: str) -> str:
    """Both the anthropic and groq SDKs raise an APIStatusError subclass with a
    .status_code attribute on a non-2xx response - duck-typed here rather than
    importing either SDK, so this works for whichever engine actually failed.
    A 429 specifically means "this engine's rate limit/quota is tapped out",
    which is common enough on Groq's free tier to deserve its own message
    instead of a raw exception dump."""
    if getattr(e, "status_code", None) == 429:
        return (
            "This engine's rate limit or daily quota has been reached. Wait a "
            "while and try again, or switch to a different engine (e.g. mock "
            "or preview) in the meantime."
        )
    return f"{label}: {' '.join(str(e).split())}"


def _warm_embedding_model():
    # The embedding model (sentence-transformers) is loaded lazily on first use -
    # nothing during ingest/seeding touches it, only a check or the document
    # network graph does. Without this, whichever user request happens to be
    # first pays that load cost inline, competing for CPU with
    # AUTO_SEED_ON_START's background thread if one's still running - the
    # direct cause of a real "Unexpected end of JSON input" (a request
    # truncated by gunicorn's timeout, not a real server error) on Render's
    # free tier. Warming it here, off the request path, means a real user's
    # first request is never the one paying for it.
    from app.retrieval import _get_model

    try:
        _get_model()
        print("Embedding model warmed", flush=True)
    except Exception as e:
        print(f"Embedding model warm-up failed (will load lazily instead): {e}", flush=True)


threading.Thread(target=_warm_embedding_model, daemon=True).start()

if os.environ.get("AUTO_SEED_ON_START"):
    # Opt-in only (unset locally, so normal dev DB state is never touched) -
    # exists for hosts like Render's free tier with no Shell access to run
    # the seed scripts by hand after a deploy. Runs in-process on the
    # deployed server itself, not from a developer's machine, so uploaded
    # seed files land on the actual STORAGE_ROOT the app will later read
    # them from - running the scripts locally against a remote DATABASE_URL
    # instead would write those files to the wrong filesystem entirely.
    # Every seed script is idempotent (checks what's already ingested), so
    # this is safe to leave on across every subsequent deploy/restart too.
    #
    # Runs in a background thread, NOT inline here at import time - Render
    # (and most PaaS hosts) kill a deploy if the process doesn't bind to a
    # port within a few minutes, and seeding Chicago's real code text alone
    # produces 3800+ clauses, comfortably enough to blow past that timeout
    # on a free-tier CPU if it has to finish before gunicorn can even start
    # listening. Confirmed this the hard way: a real deploy hung on "No open
    # ports detected" for 5+ minutes and timed out with the seeding call
    # inline here. The projects list will just look empty for the minute or
    # two seeding actually takes after a fresh deploy now, instead of the
    # whole app failing to come up at all.

    def _seed_in_background():
        from scripts import seed_demo_data, seed_jurisdictions, seed_large_demo

        # print() alone isn't enough here - stdout is block-buffered (not
        # line-buffered) once it's piped/redirected rather than an
        # interactive terminal, which is exactly gunicorn's case. Confirmed
        # directly: a background thread's print() calls didn't show up in a
        # redirected log file even 8 seconds later, while gunicorn's own
        # logging-module output flushed immediately - so without an explicit
        # flush, this thread's progress (or a silent failure) would be
        # invisible in Render's log stream for an unpredictable amount of
        # time, which is exactly the blind spot that made this hard to
        # debug the first time.
        print("AUTO_SEED_ON_START: seeding started", flush=True)
        try:
            seed_jurisdictions.main()
            seed_demo_data.main()
            seed_large_demo.main()
            print("AUTO_SEED_ON_START: seeding finished", flush=True)
        except Exception as e:
            print(f"AUTO_SEED_ON_START failed: {e}", flush=True)

    threading.Thread(target=_seed_in_background, daemon=True).start()


def get_session():
    """One SQLAlchemy session per request, closed on teardown. app/db.py's
    get_session() creates a fresh Session (and pooled connection) on every
    call with nothing to close it - fine for short-lived scripts, but every
    web route called it directly and never closed the result, leaking a
    connection per request. Under SQLite that's a real bug, not just waste:
    a connection left mid-transaction (e.g. because a request errored before
    committing) holds the file's write lock, and nothing in the process ever
    releases it - every subsequent write starts failing with
    "database is locked" until the process is restarted."""
    if "db_session" not in g:
        g.db_session = _get_session()
    return g.db_session


@app.teardown_appcontext
def _close_session(exception=None):
    session = g.pop("db_session", None)
    if session is not None:
        session.close()


def _check_status(session, project_id, has_submission, check_type):
    """(status text, CSS class) for one check_type on a project - shown on
    both the project list (at a glance) and the project page (in detail).
    Distinguishes three states a reviewer actually cares about: never
    checked, checked and clean, checked and flagged - "not checked" and
    "checked, zero flags" look identical unless this is explicit.

    Project-scoped, not submission-scoped: under incremental re-check, the
    LLMCall that most recently touched this check_type may live on an OLDER
    submission than the project's latest one, if nothing's changed since -
    filtering by the exact latest submission_id would wrongly show "not
    checked" the moment a new revision starts before anything is re-checked."""
    if not has_submission:
        return "no revision yet", "status-none"

    last_call = (
        session.query(LLMCall)
        .join(Submission, LLMCall.submission_id == Submission.id)
        .filter(Submission.project_id == project_id, LLMCall.check_type == check_type)
        .order_by(LLMCall.created_at.desc())
        .first()
    )
    if last_call is None:
        return "not checked", "status-none"

    flag_count = len(current_flags_for_project(session, project_id, check_type))
    if flag_count:
        return f"{flag_count} flag(s) ({last_call.engine})", "status-flagged"
    return f"clean ({last_call.engine})", "status-ok"


def _project_summary_rows(session):
    projects = session.query(Project).order_by(Project.name).all()
    rows = []
    for p in projects:
        latest_submission = (
            session.query(Submission)
            .filter_by(project_id=p.id)
            .order_by(Submission.sequence_number.desc())
            .first()
        )
        jurisdiction_status, jurisdiction_status_class = _check_status(session, p.id, latest_submission is not None, CheckType.JURISDICTION)
        cross_status, cross_status_class = _check_status(session, p.id, latest_submission is not None, CheckType.CROSS_DISCIPLINE)
        rows.append({
            "id": p.id,
            "name": p.name,
            "jurisdiction": p.jurisdiction.name,
            "revision": latest_submission.revision_label if latest_submission else "-",
            "sheet_count": session.query(DocumentSeries).filter_by(project_id=p.id).count(),
            "jurisdiction_status": jurisdiction_status,
            "jurisdiction_status_class": jurisdiction_status_class,
            "cross_status": cross_status,
            "cross_status_class": cross_status_class,
        })
    return rows


def _project_metrics(session, project):
    """One project's row on the metrics page.

    Deliberately does NOT compute "first-pass approval rate" or a dollar
    "rework avoided" figure - this app has no signal for actual jurisdiction
    approval/rejection outcomes or true rework cost, only what it caught
    pre-submission. Reporting those two headline numbers from data that
    can't support them would be a fabricated metric wearing a real one's
    name, so this reports only what's actually measurable: flags caught,
    reviewer-confirmed precision (once reviewers start using the status
    control on the flags page), and revision cycle time."""
    submissions = (
        session.query(Submission)
        .filter_by(project_id=project.id)
        .order_by(Submission.sequence_number)
        .all()
    )
    flags = current_flags_for_project(session, project.id)

    reviewed = [f for f in flags if f.status != FlagStatus.OPEN]
    confirmed_real = sum(1 for f in reviewed if f.status in (FlagStatus.ACKNOWLEDGED, FlagStatus.RESOLVED))
    false_positives = sum(1 for f in reviewed if f.status == FlagStatus.FALSE_POSITIVE)

    cycle_days = None
    if len(submissions) >= 2:
        cycle_days = (submissions[-1].submitted_at - submissions[0].submitted_at).days

    return {
        "id": project.id,
        "name": project.name,
        "revision_count": len(submissions),
        "cycle_days": cycle_days,
        "flags_caught": len(flags),
        "high_severity_open": sum(1 for f in flags if f.severity.value == "high" and f.status == FlagStatus.OPEN),
        "reviewed_count": len(reviewed),
        "confirmed_real": confirmed_real,
        "false_positives": false_positives,
        "precision_pct": round(100 * confirmed_real / len(reviewed)) if reviewed else None,
    }


def _severity_rank_sort(flags):
    # Sorted in Python, not SQL - severity is stored as text, so a SQL ORDER BY
    # would sort alphabetically ("medium" > "low" > "high"), not by actual severity.
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(flags, key=lambda f: severity_rank[f.severity.value])


@app.get("/")
def index():
    session = get_session()
    return render_template(
        "projects.html",
        jurisdictions=jurisdictions_with_documentation(session),
        rows=_project_summary_rows(session),
        error=request.args.get("error"),
        deleted=request.args.get("deleted"),
    )


@app.post("/projects")
def create_project():
    jurisdiction_id = request.form.get("jurisdiction_id")
    if not jurisdiction_id:
        return redirect("/?error=Select a jurisdiction (add one under Jurisdictions if none are listed)")

    session = get_session()
    project = get_or_create_project(session, request.form["project_name"], jurisdiction_id)
    session.commit()
    return redirect(f"/projects/{project.id}")


@app.post("/projects/<project_id>/delete")
def delete_project(project_id):
    """Password-gated, not tied to a real user account - there's no auth
    system yet (see CLAUDE.md's "What's actually next"), so this is a
    stopgap against anyone with the URL deleting a project, not a real
    permission model. Fails closed: if DELETE_PASSWORD isn't set on this
    deploy at all, deletion is disabled rather than silently open to
    anyone who submits an empty password."""
    configured_password = os.environ.get("DELETE_PASSWORD")
    if not configured_password:
        return redirect("/?error=Deleting projects is disabled (DELETE_PASSWORD is not configured on this server).")
    if request.form.get("password") != configured_password:
        return redirect("/?error=Incorrect password - project not deleted.")

    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    project_name = project.name
    delete_project_cascade(session, project_id)
    session.commit()
    return redirect(f"/?{urlencode({'deleted': project_name})}")


@app.get("/projects/<project_id>")
def project_detail(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    submissions = (
        session.query(Submission)
        .filter_by(project_id=project.id)
        .order_by(Submission.sequence_number.desc())
        .all()
    )
    latest_submission = submissions[0] if submissions else None

    # Current sheet set: each sheet's most recent version across ALL
    # submissions, not just ones tied to the literal latest submission_id -
    # see current_documents_for_project's docstring for why that distinction
    # matters (revising one sheet must not hide every other sheet).
    documents = sorted(
        current_documents_for_project(session, project.id),
        key=lambda d: (d.series.discipline.value, d.series.sheet_number),
    )

    present_disciplines = {d.series.discipline.value for d in documents}
    missing_disciplines = [d.value for d in Discipline if d.value not in present_disciplines]

    sheet_rows = [
        {
            "id": d.id,
            "sheet": d.series.sheet_number,
            "title": d.series.title,
            "discipline": d.series.discipline.value,
            "doc_type": d.doc_type.value,
            "filename": (d.metadata_ or {}).get("original_filename") or d.file_uri.rsplit("/", 1)[-1],
            "clause_count": session.query(DocumentClause).filter_by(document_id=d.id).count(),
            "submitted_by": d.submission.submitted_by,
            "submitted_by_is_placeholder": d.submission.submitted_by == DEFAULT_SUBMITTED_BY,
            "ingested_at": d.ingested_at.strftime("%Y-%m-%d %H:%M"),
        }
        for d in documents
    ]

    revise_sheet_number = request.args.get("revise_sheet", "")
    revise_series = find_series(session, project.id, revise_sheet_number) if revise_sheet_number else None

    jurisdiction_status, jurisdiction_status_class = _check_status(session, project.id, latest_submission is not None, CheckType.JURISDICTION)
    cross_status, cross_status_class = _check_status(session, project.id, latest_submission is not None, CheckType.CROSS_DISCIPLINE)

    submission_rows = [
        (s, submissions[i + 1].sequence_number if i + 1 < len(submissions) else None)
        for i, s in enumerate(submissions)
    ]

    return render_template(
        "project_detail.html",
        project=project,
        submissions=submissions,
        submission_rows=submission_rows,
        latest_submission=latest_submission,
        sheet_rows=sheet_rows,
        missing_disciplines=missing_disciplines,
        disciplines=[d.value for d in Discipline],
        error=request.args.get("error"),
        revising=revise_series is not None,
        prefill_sheet_number=revise_sheet_number,
        prefill_title=revise_series.title if revise_series else "",
        prefill_discipline=revise_series.discipline.value if revise_series else "",
        jurisdiction_status=jurisdiction_status,
        jurisdiction_status_class=jurisdiction_status_class,
        cross_status=cross_status,
        cross_status_class=cross_status_class,
    )


@app.get("/series-info")
def series_info():
    session = get_session()
    project = session.get(Project, request.args.get("project_id", ""))
    if project is None:
        return jsonify(None)
    series = find_series(session, project.id, request.args.get("sheet_number", ""))
    if series is None:
        return jsonify(None)
    return jsonify({"title": series.title, "discipline": series.discipline.value})


def _sheet_number_from_filename(filename: str) -> str | None:
    """A great many CAD exports are literally named after the sheet
    (A-101.pdf, S-101.pdf) - no PDF parsing needed if the filename itself
    already looks like a sheet number."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem.upper() if SHEET_NUMBER_TOKEN.match(stem) else None


def _title_from_filename(filename: str) -> str:
    """A human-readable fallback for the title field - sheet-number-based
    extraction from PDF content is unreliable enough (see
    app.clause_extraction.sniff_sheet_number's docstring) that guessing a
    title from page content risks silently filling in the wrong thing;
    the filename itself, cleaned up, is an honest best-effort prefill the
    user is expected to review and edit, not a confident extraction."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[_\-]+", " ", stem).strip()


@app.post("/sheet-info-from-file")
def sheet_info_from_file():
    """Best-effort prefill for the upload form's sheet number/title fields,
    called from the browser as soon as a file is chosen - never blocks or
    validates the actual upload, just saves re-typing what a file's own
    name/content usually already says."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"sheet_number": None, "title": None})

    try:
        doc_type = infer_doc_type(file.filename)
    except ValueError:
        return jsonify({"sheet_number": None, "title": _title_from_filename(file.filename)})

    content = file.read()
    sheet_number = sniff_sheet_number(content, doc_type) or _sheet_number_from_filename(file.filename)
    return jsonify({"sheet_number": sheet_number, "title": _title_from_filename(file.filename)})


@app.post("/projects/<project_id>/ingest")
def ingest(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    file = request.files.get("file")
    if not file or not file.filename:
        return redirect(f"/projects/{project_id}?error=No file selected")

    try:
        doc_type = infer_doc_type(file.filename)
    except ValueError as e:
        return redirect(f"/projects/{project_id}?{urlencode({'error': str(e)})}")

    submission = get_latest_or_create_submission(session, project.id)
    sheet_number = request.form["sheet_number"]

    # Document has a (document_series_id, submission_id) unique constraint -
    # one version of a sheet per revision, by design (see app/models.py) -
    # so uploading a sheet number that's already in the current revision
    # would otherwise crash with a raw IntegrityError/500 instead of telling
    # the user what to do about it (start a new revision, or use
    # /documents/<id>/revise, which does exactly that pre-filled).
    existing_series = find_series(session, project.id, sheet_number)
    if existing_series is not None:
        already_in_revision = (
            session.query(Document)
            .filter_by(document_series_id=existing_series.id, submission_id=submission.id)
            .first()
        )
        if already_in_revision is not None:
            message = (
                f"Sheet {sheet_number} already exists in {submission.revision_label}. "
                f"Start a new revision to upload an updated version, or use a different sheet number."
            )
            return redirect(f"/projects/{project_id}?{urlencode({'error': message})}")

    document = ingest_document(
        session,
        submission,
        sheet_number=sheet_number,
        discipline=Discipline(request.form["discipline"]),
        title=request.form["title"],
        doc_type=doc_type,
        content=file.read(),
        filename=file.filename,
    )
    extract_and_store_clauses(session, document)
    session.commit()
    return redirect(f"/projects/{project_id}")


@app.post("/projects/<project_id>/new-revision")
def new_revision(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")
    create_submission(session, project.id)
    session.commit()
    return redirect(f"/projects/{project_id}")


@app.post("/documents/<document_id>/revise")
def revise_document(document_id):
    """Starts a new revision for this document's project and sends the user
    back to that project's page pre-filled with this sheet's info, so
    uploading the updated file is the only thing left to do. Always starts
    a fresh revision (not conditionally) - re-uploading into the same
    revision this document already belongs to would violate the
    (document_series_id, submission_id) uniqueness constraint on Document."""
    session = get_session()
    document = session.get(Document, document_id)
    if document is None:
        return redirect("/?error=Document not found")

    series = document.series
    create_submission(session, series.project_id)
    session.commit()

    return redirect(f"/projects/{series.project_id}?{urlencode({'revise_sheet': series.sheet_number})}")


@app.post("/projects/<project_id>/check")
def check(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    if _is_check_running(project_id, CheckType.JURISDICTION.value):
        return redirect(f"/projects/{project_id}?error=A vs. code check is already running for this project.")

    submission = get_latest_or_create_submission(session, project.id)
    session.commit()
    engine = request.form.get("engine", "mock")
    force = request.form.get("force") == "1"
    submission_id = submission.id

    _start_check_tracking(project_id, CheckType.JURISDICTION.value)

    def _run():
        # A fresh session, not the request-scoped one above - that one gets
        # closed by Flask's teardown handler right after this view returns,
        # well before this background thread's work is done.
        bg_session = _get_session()
        try:
            bg_submission = bg_session.get(Submission, submission_id)
            check_submission_for_conflicts(bg_session, bg_submission, engine=engine, force=force)
            bg_session.commit()
            _finish_check_tracking(project_id, CheckType.JURISDICTION.value)
        except Exception as e:
            bg_session.rollback()
            _finish_check_tracking(
                project_id, CheckType.JURISDICTION.value, error=_describe_check_error(e, "Check failed")
            )
        finally:
            bg_session.close()

    threading.Thread(target=_run, daemon=True).start()
    return redirect(f"/projects/{project_id}?checking=jurisdiction")


@app.get("/projects/<project_id>/check-status")
def check_status(project_id):
    check_type_value = request.args.get("type", "jurisdiction")
    return jsonify({
        "running": _is_check_running(project_id, check_type_value),
        "error": _check_errors.get((project_id, check_type_value)),
    })


@app.post("/projects/<project_id>/check-cross-discipline")
def check_cross_discipline(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    engine = request.form.get("engine", "preview")
    if engine == "preview":
        # No mock option here (unlike the jurisdiction check) - empirically,
        # its keyword heuristic essentially never fires on cross-discipline
        # pairs, since coordination clashes routinely share an incidental
        # room/corridor number even when the numbers that actually matter
        # (a clearance dimension) differ, and the heuristic only looks for
        # "any number in common." A fabricated "no conflicts found" is worse
        # than an honest "here's what retrieval matched, unjudged" - so the
        # no-LLM path goes straight to the candidates preview instead.
        return redirect(f"/projects/{project_id}/cross-discipline-candidates")

    if _is_check_running(project_id, CheckType.CROSS_DISCIPLINE.value):
        return redirect(f"/projects/{project_id}?error=A cross-discipline check is already running for this project.")

    submission = get_latest_or_create_submission(session, project.id)
    session.commit()
    force = request.form.get("force") == "1"
    submission_id = submission.id

    _start_check_tracking(project_id, CheckType.CROSS_DISCIPLINE.value)

    def _run():
        bg_session = _get_session()
        try:
            bg_submission = bg_session.get(Submission, submission_id)
            check_submission_for_cross_discipline_conflicts(bg_session, bg_submission, engine=engine, force=force)
            bg_session.commit()
            _finish_check_tracking(project_id, CheckType.CROSS_DISCIPLINE.value)
        except Exception as e:
            bg_session.rollback()
            _finish_check_tracking(
                project_id,
                CheckType.CROSS_DISCIPLINE.value,
                error=_describe_check_error(e, "Cross-discipline check failed"),
            )
        finally:
            bg_session.close()

    threading.Thread(target=_run, daemon=True).start()
    return redirect(f"/projects/{project_id}?checking=cross_discipline")


@app.get("/projects/<project_id>/cross-discipline-candidates")
def cross_discipline_candidates(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    submission = get_latest_or_create_submission(session, project.id)
    clauses = current_project_clauses(session, project.id)
    candidates_by_clause = find_candidate_cross_discipline_clauses_scored(clauses)
    rows = [(clause, candidates_by_clause.get(clause.id, [])) for clause in clauses]

    return render_template(
        "cross_discipline_candidates.html",
        project=project,
        revision=submission.revision_label,
        rows=rows,
    )


@app.get("/projects/<project_id>/document-graph")
def project_document_graph(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return jsonify({"error": "Project not found"}), 404
    return jsonify(document_graph_data(session, project_id))


@app.get("/projects/<project_id>/diff")
def project_diff(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    try:
        from_seq = int(request.args["from"])
        to_seq = int(request.args["to"])
    except (KeyError, ValueError):
        return redirect(f"/projects/{project_id}?error=Invalid diff range")

    from_submission = session.query(Submission).filter_by(project_id=project.id, sequence_number=from_seq).first()
    to_submission = session.query(Submission).filter_by(project_id=project.id, sequence_number=to_seq).first()
    if from_submission is None or to_submission is None:
        return redirect(f"/projects/{project_id}?error=Revision not found")

    sheets = diff_between_submissions(session, project.id, from_seq, to_seq)
    sheets.sort(key=lambda s: (s["series"].discipline.value, s["series"].sheet_number))

    return render_template(
        "diff.html",
        project=project,
        from_revision=from_submission.revision_label,
        to_revision=to_submission.revision_label,
        sheets=sheets,
    )


@app.get("/projects/<project_id>/audit-report")
def project_audit_report(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    flags = _severity_rank_sort(current_flags_for_project(session, project_id))
    sheet_count = session.query(DocumentSeries).filter_by(project_id=project_id).count()

    return render_template(
        "audit_report.html",
        project=project,
        flags=flags,
        sheet_count=sheet_count,
        high_count=sum(1 for f in flags if f.severity.value == "high"),
        medium_count=sum(1 for f in flags if f.severity.value == "medium"),
        low_count=sum(1 for f in flags if f.severity.value == "low"),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.get("/projects/<project_id>/audit-report.csv")
def project_audit_report_csv(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    flags = _severity_rank_sort(current_flags_for_project(session, project_id))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "type", "severity", "discipline", "sheet", "clause_label", "explanation",
        "cited", "model", "is_simulated", "revision", "checked_at",
    ])
    for f in flags:
        cited = "; ".join(
            (c.jurisdiction_clause.clause_label if c.jurisdiction_clause else
             f"{c.clause.series.discipline.value} {c.clause.clause_label}")
            for c in f.citations
        )
        writer.writerow([
            "cross_discipline" if f.check_type.value == "cross_discipline" else "jurisdiction",
            f.severity.value,
            f.clause.series.discipline.value,
            f.clause.series.sheet_number,
            f.clause.clause_label,
            f.explanation,
            cited,
            f.model,
            f.is_simulated,
            f.submission.revision_label,
            f.llm_call.created_at.isoformat() if f.llm_call else "",
        ])

    filename = f"{project.name.replace(' ', '_')}_audit_report.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/submissions/<submission_id>/flags")
def submission_flags(submission_id):
    """Historical link target (e.g. from a Flag's reasoning-trace back
    link) - flags are a project-level "current state" concept now, not a
    per-submission snapshot, so this just forwards to that view."""
    session = get_session()
    submission = session.get(Submission, submission_id)
    if submission is None:
        return redirect("/?error=Submission not found")
    return redirect(f"/projects/{submission.project_id}/flags")


@app.get("/projects/<project_id>/flags")
def project_flags(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    type_filter = request.args.get("type", "all")
    check_type = {"jurisdiction": CheckType.JURISDICTION, "cross_discipline": CheckType.CROSS_DISCIPLINE}.get(type_filter)
    flags = _severity_rank_sort(current_flags_for_project(session, project_id, check_type))
    all_flags = flags if check_type is None else current_flags_for_project(session, project_id)
    return render_template(
        "flags.html",
        project=project.name,
        project_id=project.id,
        flags=flags,
        type_filter=type_filter,
        jurisdiction_count=sum(1 for f in all_flags if f.check_type == CheckType.JURISDICTION),
        cross_discipline_count=sum(1 for f in all_flags if f.check_type == CheckType.CROSS_DISCIPLINE),
    )


@app.get("/metrics")
def metrics():
    session = get_session()
    projects = session.query(Project).order_by(Project.name).all()
    rows = [_project_metrics(session, p) for p in projects]

    all_flags_total = sum(r["flags_caught"] for r in rows)
    all_reviewed = sum(r["reviewed_count"] for r in rows)
    all_confirmed_real = sum(r["confirmed_real"] for r in rows)
    portfolio_precision_pct = round(100 * all_confirmed_real / all_reviewed) if all_reviewed else None

    return render_template(
        "metrics.html",
        rows=rows,
        all_flags_total=all_flags_total,
        all_reviewed=all_reviewed,
        portfolio_precision_pct=portfolio_precision_pct,
    )


@app.post("/flags/<flag_id>/status")
def update_flag_status(flag_id):
    """Records a reviewer's disposition of a flag - open/acknowledged/resolved/
    false positive, plus an optional note. This is the "pilot alongside human
    review" hook: it doesn't compare against an independent review pass, but
    it does let a reviewer mark what the tool got right or wrong, which the
    metrics view then rolls up into a reviewer-confirmed precision figure."""
    session = get_session()
    flag = session.get(Flag, flag_id)
    if flag is None:
        return redirect("/?error=Flag not found")

    try:
        flag.status = FlagStatus(request.form["status"])
    except ValueError:
        return redirect(f"/projects/{flag.submission.project_id}/flags?error=Invalid status")
    flag.status_note = request.form.get("status_note") or None
    flag.status_updated_at = datetime.now(timezone.utc)
    session.commit()
    return redirect(f"/projects/{flag.submission.project_id}/flags")


@app.get("/flags/<flag_id>/reasoning")
def flag_reasoning(flag_id):
    session = get_session()
    flag = session.get(Flag, flag_id)
    if flag is None:
        return redirect("/?error=Flag not found")
    return render_template("reasoning.html", flag=flag, llm_call=flag.llm_call)


@app.get("/jurisdictions")
def jurisdictions():
    session = get_session()
    all_jurisdictions = session.query(Jurisdiction).order_by(Jurisdiction.name).all()
    documents = (
        session.query(JurisdictionDocument)
        .join(Jurisdiction)
        .order_by(JurisdictionDocument.ingested_at.desc())
        .all()
    )
    rows = [
        {
            "id": d.id,
            "jurisdiction_name": d.jurisdiction.name,
            "title": d.title,
            "doc_type": d.doc_type.value,
            "clause_count": session.query(JurisdictionClause).filter_by(jurisdiction_document_id=d.id).count(),
            "ingested_at": d.ingested_at.strftime("%Y-%m-%d %H:%M"),
        }
        for d in documents
    ]
    return render_template(
        "jurisdictions.html",
        jurisdictions=all_jurisdictions,
        rows=rows,
        error=request.args.get("error"),
    )


@app.post("/jurisdictions/documents")
def add_jurisdiction_document():
    # The jurisdiction setup workflow normally starts with a small stack of
    # related sources (base code, state/local amendments, permit guides), so
    # accepting only one document per request makes initial setup needlessly
    # repetitive.  Keep the legacy `file`/`title` fields working while the UI
    # sends `files`/`titles` for a batch.
    files = [file for file in request.files.getlist("files") if file and file.filename]
    if not files:
        legacy_file = request.files.get("file")
        if legacy_file and legacy_file.filename:
            files = [legacy_file]
    if not files:
        return redirect("/jurisdictions?error=No file selected")

    # Validate the whole batch before writing anything, so a bad file does
    # not leave the user with a surprising half-ingested jurisdiction.
    typed_files = []
    for file in files:
        try:
            typed_files.append((file, infer_doc_type(file.filename)))
        except ValueError as e:
            return redirect(f"/jurisdictions?{urlencode({'error': str(e)})}")

    session = get_session()
    jurisdiction = get_or_create_jurisdiction(session, request.form["jurisdiction_name"])
    titles = request.form.getlist("titles")
    legacy_title = request.form.get("title", "").strip()

    for index, (file, doc_type) in enumerate(typed_files):
        title = titles[index].strip() if index < len(titles) else ""
        if not title:
            title = legacy_title if len(typed_files) == 1 and legacy_title else _title_from_filename(file.filename)
        document = ingest_jurisdiction_document(
            session,
            jurisdiction,
            title=title,
            doc_type=doc_type,
            content=file.read(),
            filename=file.filename,
        )
        extract_and_store_jurisdiction_clauses(session, document)
    session.commit()
    return redirect("/jurisdictions")


@app.get("/jurisdictions/documents/<document_id>/clauses")
def jurisdiction_document_clauses(document_id):
    session = get_session()
    document = session.get(JurisdictionDocument, document_id)
    if document is None:
        return redirect("/jurisdictions?error=Document not found")

    total_count = session.query(JurisdictionClause).filter_by(jurisdiction_document_id=document_id).count()
    query = session.query(JurisdictionClause).filter_by(jurisdiction_document_id=document_id)
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(
            (JurisdictionClause.text.ilike(f"%{q}%")) | (JurisdictionClause.clause_label.ilike(f"%{q}%"))
        )
    clauses = query.order_by(JurisdictionClause.clause_label).all()

    return render_template(
        "clauses.html",
        back_href="/jurisdictions",
        sheet_number=document.jurisdiction.name,
        title=document.title,
        project="(jurisdiction reference document)",
        revision="-",
        discipline="-",
        doc_type=document.doc_type.value,
        clauses=clauses,
        total_count=total_count,
        q=q,
    )


@app.get("/documents/<document_id>/clauses")
def document_clauses(document_id):
    session = get_session()
    document = session.get(Document, document_id)
    if document is None:
        return redirect("/?error=Document not found")

    base_query = (
        session.query(Clause)
        .join(DocumentClause, DocumentClause.clause_id == Clause.id)
        .filter(DocumentClause.document_id == document_id)
    )
    total_count = base_query.count()
    query = base_query
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter((Clause.text.ilike(f"%{q}%")) | (Clause.clause_label.ilike(f"%{q}%")))
    clauses = query.order_by(Clause.clause_label).all()

    return render_template(
        "clauses.html",
        back_href=f"/projects/{document.series.project_id}",
        sheet_number=document.series.sheet_number,
        title=document.series.title,
        project=document.series.project.name,
        revision=document.submission.revision_label,
        discipline=document.series.discipline.value,
        doc_type=document.doc_type.value,
        clauses=clauses,
        total_count=total_count,
        q=q,
    )


@app.get("/files/<document_id>")
def download(document_id):
    session = get_session()
    document = session.get(Document, document_id)
    if document is None:
        return redirect("/?error=Document not found")

    content = storage.load(document.file_uri)
    filename = (document.metadata_ or {}).get("original_filename") or document.file_uri.rsplit("/", 1)[-1]
    return Response(
        content,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
