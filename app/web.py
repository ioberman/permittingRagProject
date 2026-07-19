# Very simple web UI for ingesting documents, for local use only. Not intended to be a full-featured document management system.

"""Minimal local web UI for ingesting documents: upload a file, see it in the list.
Field count is kept to what a reviewer actually knows off the top of their
head - project, jurisdiction, sheet number, title, discipline, file. Revision
and document type are derived rather than asked:
  - revision: uploads default to the project's latest revision, so a batch of
    sheets naturally groups together. Starting a new revision is a separate,
    explicit action, not a per-upload field.
  - document type: inferred from the file extension.
"""

from urllib.parse import urlencode

from flask import Flask, jsonify, redirect, render_template_string, request, Response

from app.clause_extraction import extract_and_store_clauses, extract_and_store_jurisdiction_clauses
from app.db import get_session, init_db
from app.ingest import (
    DEFAULT_SUBMITTED_BY,
    find_project,
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
    Clause,
    Discipline,
    Document,
    DocumentClause,
    DocumentSeries,
    Jurisdiction,
    JurisdictionClause,
    JurisdictionDocument,
    Project,
    Submission,
)

app = Flask(__name__)
init_db()

PAGE = """
<!doctype html>
<title>Plan Review Copilot - Ingest</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  fieldset { margin-bottom: 1.5rem; }
  label { display: block; margin-top: 0.75rem; font-size: 0.9rem; }
  input, select { width: 100%; padding: 0.4rem; margin-top: 0.2rem; box-sizing: border-box; }
  button { margin-top: 1rem; padding: 0.5rem 1rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; font-size: 0.85rem; }
  .error { color: #b00020; }
  .new-revision { display: flex; gap: 0.5rem; align-items: flex-end; margin-bottom: 2rem; }
  .new-revision label { flex: 1; margin-top: 0; }
  .new-revision button { margin-top: 0; }
  .placeholder-badge {
    display: inline-block; font-size: 0.7rem; background: #fff3cd; color: #856404;
    padding: 0.05rem 0.4rem; border-radius: 3px; margin-left: 0.4rem; cursor: help;
  }
  .autofilled { background: #eef7ee; }
  .nav { margin-bottom: 1.5rem; }
  .nav a { margin-right: 1rem; }
  .info { background: #eef7ee; border: 1px solid #bfe0bf; padding: 0.6rem 0.9rem; border-radius: 3px; font-size: 0.9rem; }
  .revise-form { display: inline; }
  .revise-form button { margin: 0; padding: 0.15rem 0.5rem; font-size: 0.8rem; }
</style>

<div class="nav"><a href="/jurisdictions">Manage jurisdictions</a></div>

<h1>Ingest a document</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
{% if revising %}
<p class="info">Revising <strong>{{ prefill_sheet_number }}</strong> ({{ prefill_project }}) as a new revision - pick the updated file below.</p>
{% endif %}

{% if not jurisdictions %}
<p class="error">No jurisdictions have reference documentation loaded yet -
  <a href="/jurisdictions">add one</a> before ingesting a project document.</p>
{% endif %}

<form method="post" action="/new-revision" class="new-revision">
  <label>Start a new revision for
    <select name="project_name">
      {% for p in projects %}<option value="{{ p.name }}">{{ p.name }}</option>{% endfor %}
    </select>
  </label>
  <button type="submit">Start new revision</button>
</form>

<form method="post" action="/ingest" enctype="multipart/form-data">
  <fieldset>
    <label>Project name
      <input id="project_name" name="project_name" list="projects" value="{{ prefill_project }}" required>
      <datalist id="projects">
        {% for p in projects %}<option value="{{ p.name }}">{% endfor %}
      </datalist>
    </label>
    <label>Jurisdiction (only used if project is new; only jurisdictions with loaded documentation are listed)
      <select name="jurisdiction_id" {% if not jurisdictions %}disabled{% endif %} required>
        {% for j in jurisdictions %}<option value="{{ j.id }}" {% if j.id == prefill_jurisdiction_id %}selected{% endif %}>{{ j.name }}</option>{% endfor %}
      </select>
    </label>
    <label>Sheet number
      <input id="sheet_number" name="sheet_number" value="{{ prefill_sheet_number }}" required>
    </label>
    <label id="title_label">Title
      <input id="title" name="title" value="{{ prefill_title }}" required>
    </label>
    <label id="discipline_label">Discipline
      <select id="discipline" name="discipline">
        {% for d in disciplines %}<option value="{{ d }}" {% if d == prefill_discipline %}selected{% endif %}>{{ d }}</option>{% endfor %}
      </select>
    </label>
    <label>File
      <input type="file" name="file" required>
    </label>
    <button type="submit">Ingest</button>
  </fieldset>
</form>

<script>
  async function autofillFromExistingSheet() {
    const project = document.getElementById('project_name').value.trim();
    const sheet = document.getElementById('sheet_number').value.trim();
    const titleInput = document.getElementById('title');
    const disciplineSelect = document.getElementById('discipline');
    titleInput.classList.remove('autofilled');
    disciplineSelect.classList.remove('autofilled');
    if (!project || !sheet) return;

    const res = await fetch(`/series-info?project_name=${encodeURIComponent(project)}&sheet_number=${encodeURIComponent(sheet)}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data) return;

    titleInput.value = data.title;
    disciplineSelect.value = data.discipline;
    titleInput.classList.add('autofilled');
    disciplineSelect.classList.add('autofilled');
  }
  document.getElementById('sheet_number').addEventListener('blur', autofillFromExistingSheet);
  document.getElementById('project_name').addEventListener('blur', autofillFromExistingSheet);
</script>

<h2>Ingested documents</h2>
<table>
  <tr><th>Project</th><th>Revision</th><th>Sheet</th><th>Discipline</th><th>Type</th><th>File</th><th>Clauses</th><th>Submitted by</th><th>Ingested</th><th></th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.project }}</td>
    <td>{{ row.revision }}</td>
    <td>{{ row.sheet }}</td>
    <td>{{ row.discipline }}</td>
    <td>{{ row.doc_type }}</td>
    <td><a href="/files/{{ row.id }}">{{ row.filename }}</a></td>
    <td>
      {% if row.clause_count %}<a href="/documents/{{ row.id }}/clauses">{{ row.clause_count }}</a>{% else %}0{% endif %}
    </td>
    <td>
      {{ row.submitted_by }}
      {% if row.submitted_by_is_placeholder %}
      <span class="placeholder-badge" title="Hardcoded until web auth is wired up - will show the real logged-in user">placeholder</span>
      {% endif %}
    </td>
    <td>{{ row.ingested_at }}</td>
    <td>
      <form method="post" action="/documents/{{ row.id }}/revise" class="revise-form">
        <button type="submit" title="Start a new revision pre-filled with this sheet's info">Revise</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
"""

JURISDICTIONS_PAGE = """
<!doctype html>
<title>Jurisdictions - Plan Review Copilot</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  fieldset { margin-bottom: 1.5rem; }
  label { display: block; margin-top: 0.75rem; font-size: 0.9rem; }
  input, select { width: 100%; padding: 0.4rem; margin-top: 0.2rem; box-sizing: border-box; }
  button { margin-top: 1rem; padding: 0.5rem 1rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; font-size: 0.85rem; }
  .error { color: #b00020; }
</style>

<p><a href="/">&larr; back</a></p>
<h1>Jurisdictions</h1>
<p>Only jurisdictions listed here (with at least one document loaded) are selectable when ingesting a project document.</p>
{% if error %}<p class="error">{{ error }}</p>{% endif %}

<form method="post" action="/jurisdictions/documents" enctype="multipart/form-data">
  <fieldset>
    <label>Jurisdiction name
      <input name="jurisdiction_name" list="jurisdiction_names" required>
      <datalist id="jurisdiction_names">
        {% for j in jurisdictions %}<option value="{{ j.name }}">{% endfor %}
      </datalist>
    </label>
    <label>Document title
      <input name="title" required>
    </label>
    <label>File (code book, amendment, etc.)
      <input type="file" name="file" required>
    </label>
    <button type="submit">Add reference document</button>
  </fieldset>
</form>

<h2>Loaded jurisdictions</h2>
<table>
  <tr><th>Jurisdiction</th><th>Document</th><th>Type</th><th>Clauses</th><th>Ingested</th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.jurisdiction_name }}</td>
    <td>{{ row.title }}</td>
    <td>{{ row.doc_type }}</td>
    <td>
      {% if row.clause_count %}<a href="/jurisdictions/documents/{{ row.id }}/clauses">{{ row.clause_count }}</a>{% else %}0{% endif %}
    </td>
    <td>{{ row.ingested_at }}</td>
  </tr>
  {% endfor %}
</table>
"""

CLAUSES_PAGE = """
<!doctype html>
<title>Clauses - {{ sheet_number }} - Plan Review Copilot</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; font-size: 0.85rem; vertical-align: top; }
  .label { font-family: ui-monospace, monospace; white-space: nowrap; }
  .meta { color: #666; font-size: 0.85rem; }
</style>

<p><a href="/">&larr; back</a></p>
<h1>{{ sheet_number }} &mdash; {{ title }}</h1>
<p class="meta">{{ project }} / {{ revision }} / {{ discipline }} / {{ doc_type }}</p>

<table>
  <tr><th>Label</th><th>Text</th><th>Page</th><th>Extraction method</th></tr>
  {% for c in clauses %}
  <tr>
    <td class="label">{{ c.clause_label }}</td>
    <td>{{ c.text }}</td>
    <td>{{ c.location.get('page', '-') }}</td>
    <td>{{ c.extraction_method.value }}</td>
  </tr>
  {% endfor %}
</table>
"""


def _recent_rows(session):
    documents = (
        session.query(Document)
        .join(DocumentSeries)
        .join(Submission)
        .join(Project)
        .order_by(Document.ingested_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": d.id,
            "project": d.series.project.name,
            "revision": d.submission.revision_label,
            "sheet": d.series.sheet_number,
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


@app.get("/")
def index():
    session = get_session()
    projects = session.query(Project).order_by(Project.name).all()
    return render_template_string(
        PAGE,
        projects=projects,
        jurisdictions=jurisdictions_with_documentation(session),
        disciplines=[d.value for d in Discipline],
        rows=_recent_rows(session),
        error=request.args.get("error"),
        prefill_project=request.args.get("project", ""),
        prefill_jurisdiction_id=request.args.get("jurisdiction_id", ""),
        prefill_sheet_number=request.args.get("sheet_number", ""),
        prefill_title=request.args.get("title", ""),
        prefill_discipline=request.args.get("discipline", ""),
        revising=bool(request.args.get("sheet_number")),
    )


@app.get("/series-info")
def series_info():
    session = get_session()
    project = find_project(session, request.args.get("project_name", ""))
    if project is None:
        return jsonify(None)
    series = find_series(session, project.id, request.args.get("sheet_number", ""))
    if series is None:
        return jsonify(None)
    return jsonify({"title": series.title, "discipline": series.discipline.value})


@app.post("/ingest")
def ingest():
    file = request.files.get("file")
    if not file or not file.filename:
        return redirect("/?error=No file selected")

    try:
        doc_type = infer_doc_type(file.filename)
    except ValueError as e:
        return redirect(f"/?error={e}")

    jurisdiction_id = request.form.get("jurisdiction_id")
    if not jurisdiction_id:
        return redirect("/?error=Select a jurisdiction (add one under Manage jurisdictions if none are listed)")

    session = get_session()
    project = get_or_create_project(session, request.form["project_name"], jurisdiction_id)
    submission = get_latest_or_create_submission(session, project.id)
    document = ingest_document(
        session,
        submission,
        sheet_number=request.form["sheet_number"],
        discipline=Discipline(request.form["discipline"]),
        title=request.form["title"],
        doc_type=doc_type,
        content=file.read(),
        filename=file.filename,
    )
    extract_and_store_clauses(session, document)
    session.commit()
    return redirect("/")


@app.post("/new-revision")
def new_revision():
    session = get_session()
    project = find_project(session, request.form["project_name"])
    if project is None:
        return redirect("/?error=Unknown project")
    create_submission(session, project.id)
    session.commit()
    return redirect(f"/?project={project.name}")


@app.post("/documents/<document_id>/revise")
def revise_document(document_id):
    """Starts a new revision for this document's project and sends the user
    back to the ingest form pre-filled with everything about this sheet,
    so uploading the updated file is the only thing left to do. Always starts
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

    params = {
        "project": series.project.name,
        "jurisdiction_id": series.project.jurisdiction_id,
        "sheet_number": series.sheet_number,
        "title": series.title,
        "discipline": series.discipline.value,
    }
    return redirect(f"/?{urlencode(params)}")


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
    return render_template_string(
        JURISDICTIONS_PAGE,
        jurisdictions=all_jurisdictions,
        rows=rows,
        error=request.args.get("error"),
    )


@app.post("/jurisdictions/documents")
def add_jurisdiction_document():
    file = request.files.get("file")
    if not file or not file.filename:
        return redirect("/jurisdictions?error=No file selected")

    try:
        doc_type = infer_doc_type(file.filename)
    except ValueError as e:
        return redirect(f"/jurisdictions?error={e}")

    session = get_session()
    jurisdiction = get_or_create_jurisdiction(session, request.form["jurisdiction_name"])
    document = ingest_jurisdiction_document(
        session,
        jurisdiction,
        title=request.form["title"],
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

    clauses = (
        session.query(JurisdictionClause)
        .filter_by(jurisdiction_document_id=document_id)
        .order_by(JurisdictionClause.clause_label)
        .all()
    )
    return render_template_string(
        CLAUSES_PAGE,
        sheet_number=document.jurisdiction.name,
        title=document.title,
        project="(jurisdiction reference document)",
        revision="-",
        discipline="-",
        doc_type=document.doc_type.value,
        clauses=clauses,
    )


@app.get("/documents/<document_id>/clauses")
def document_clauses(document_id):
    session = get_session()
    document = session.get(Document, document_id)
    if document is None:
        return redirect("/?error=Document not found")

    clauses = (
        session.query(Clause)
        .join(DocumentClause, DocumentClause.clause_id == Clause.id)
        .filter(DocumentClause.document_id == document_id)
        .order_by(Clause.clause_label)
        .all()
    )
    return render_template_string(
        CLAUSES_PAGE,
        sheet_number=document.series.sheet_number,
        title=document.series.title,
        project=document.series.project.name,
        revision=document.submission.revision_label,
        discipline=document.series.discipline.value,
        doc_type=document.doc_type.value,
        clauses=clauses,
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
