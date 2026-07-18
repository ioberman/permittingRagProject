"""Minimal local web UI for ingesting documents: upload a file, see it in the list.

Field count is kept to what a reviewer actually knows off the top of their
head - project, jurisdiction, sheet number, title, discipline, file. Revision
and document type are derived rather than asked:
  - revision: uploads default to the project's latest revision, so a batch of
    sheets naturally groups together. Starting a new revision is a separate,
    explicit action, not a per-upload field.
  - document type: inferred from the file extension.
"""

from flask import Flask, jsonify, redirect, render_template_string, request, Response

from app.db import get_session, init_db
from app.ingest import (
    DEFAULT_SUBMITTED_BY,
    find_project,
    find_series,
    get_latest_or_create_submission,
    get_or_create_project,
    create_submission,
    ingest_document,
    infer_doc_type,
    storage,
)
from app.models import Discipline, Document, DocumentSeries, Project, Submission

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
</style>

<h1>Ingest a document</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}

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
    <label>Jurisdiction (only used if project is new)
      <input name="jurisdiction">
    </label>
    <label>Sheet number
      <input id="sheet_number" name="sheet_number" required>
    </label>
    <label id="title_label">Title
      <input id="title" name="title" required>
    </label>
    <label id="discipline_label">Discipline
      <select id="discipline" name="discipline">
        {% for d in disciplines %}<option value="{{ d }}">{{ d }}</option>{% endfor %}
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
  <tr><th>Project</th><th>Revision</th><th>Sheet</th><th>Discipline</th><th>Type</th><th>File</th><th>Submitted by</th><th>Ingested</th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.project }}</td>
    <td>{{ row.revision }}</td>
    <td>{{ row.sheet }}</td>
    <td>{{ row.discipline }}</td>
    <td>{{ row.doc_type }}</td>
    <td><a href="/files/{{ row.id }}">{{ row.filename }}</a></td>
    <td>
      {{ row.submitted_by }}
      {% if row.submitted_by_is_placeholder %}
      <span class="placeholder-badge" title="Hardcoded until web auth is wired up - will show the real logged-in user">placeholder</span>
      {% endif %}
    </td>
    <td>{{ row.ingested_at }}</td>
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
            "filename": d.file_uri.rsplit("_", 1)[-1],
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
        disciplines=[d.value for d in Discipline],
        rows=_recent_rows(session),
        error=request.args.get("error"),
        prefill_project=request.args.get("project", ""),
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

    session = get_session()
    project = get_or_create_project(
        session, request.form["project_name"], request.form.get("jurisdiction", "")
    )
    submission = get_latest_or_create_submission(session, project.id)
    ingest_document(
        session,
        submission,
        sheet_number=request.form["sheet_number"],
        discipline=Discipline(request.form["discipline"]),
        title=request.form["title"],
        doc_type=doc_type,
        content=file.read(),
        filename=file.filename,
    )
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


@app.get("/files/<document_id>")
def download(document_id):
    session = get_session()
    document = session.get(Document, document_id)
    if document is None:
        return redirect("/?error=Document not found")

    content = storage.load(document.file_uri)
    filename = document.file_uri.rsplit("_", 1)[-1]
    return Response(
        content,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
