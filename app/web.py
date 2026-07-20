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
"""

import csv
import io
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import Flask, g, jsonify, redirect, render_template_string, request, Response

from app.check_persistence import (
    current_documents_for_project,
    current_flags_for_project,
    current_project_clauses,
    diff_between_submissions,
)
from app.clause_extraction import extract_and_store_clauses, extract_and_store_jurisdiction_clauses
from app.conflict_detection import check_submission_for_conflicts
from app.cross_discipline_detection import check_submission_for_cross_discipline_conflicts
from app.db import get_session as _get_session
from app.db import init_db
from app.ingest import (
    DEFAULT_SUBMITTED_BY,
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
    Jurisdiction,
    JurisdictionClause,
    JurisdictionDocument,
    LLMCall,
    Project,
    Submission,
)
from app.retrieval import find_candidate_cross_discipline_clauses_scored

app = Flask(__name__)
init_db()


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


# ---------------------------------------------------------------------------
# Shared page chrome - a plain string, not a Jinja include, since these
# templates are Python string constants rather than files. Concatenated with
# `+`, not f-strings, so Jinja's {{ }} syntax in the page bodies is never at
# risk of colliding with Python's { } f-string escaping.
# ---------------------------------------------------------------------------

BASE_STYLE = """
  body { font-family: system-ui, sans-serif; max-width: 1000px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { font-size: 1.4rem; margin-bottom: 0.3rem; }
  h2 { font-size: 1.05rem; margin-top: 2rem; margin-bottom: 0.5rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; font-size: 0.85rem; vertical-align: top; }
  a { color: #2557a7; }
  .label { font-family: ui-monospace, monospace; white-space: nowrap; }
  .meta { color: #666; font-size: 0.85rem; margin-bottom: 1rem; }
  .error { color: #b00020; }
  .info { background: #eef4fb; border: 1px solid #bcd6ee; padding: 0.6rem 0.9rem; border-radius: 3px; font-size: 0.9rem; margin-bottom: 1.5rem; }
  .warn { background: #fff8e6; border: 1px solid #f0dca0; padding: 0.6rem 0.9rem; border-radius: 3px; font-size: 0.9rem; margin-bottom: 1.5rem; }
  .nav { margin-bottom: 1.5rem; font-size: 0.9rem; }
  .nav a { margin-right: 1rem; }
  .placeholder-badge { display: inline-block; font-size: 0.7rem; background: #fff3cd; color: #856404; padding: 0.05rem 0.4rem; border-radius: 3px; margin-left: 0.4rem; cursor: help; }
  .sev-high { color: #b00020; font-weight: 600; }
  .sev-medium { color: #a15c00; font-weight: 600; }
  .sev-low { color: #555; }
  .status-ok { color: #1a7a1a; }
  .status-flagged { color: #b00020; font-weight: 600; }
  .status-none { color: #888; }
  fieldset { border: 1px solid #ddd; border-radius: 4px; margin-bottom: 1.5rem; }
  label { display: block; margin-top: 0.75rem; font-size: 0.9rem; }
  input, select { padding: 0.4rem; margin-top: 0.2rem; box-sizing: border-box; }
  button { padding: 0.4rem 0.9rem; cursor: pointer; }
  .autofilled { background: #eef7ee; }
  .actions { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; margin-bottom: 1.5rem; }
  .actions form { display: flex; gap: 0.4rem; align-items: center; background: #f7f7f7; padding: 0.5rem 0.75rem; border-radius: 4px; margin: 0; }
  .actions label { margin: 0; font-size: 0.8rem; color: #555; }
  .actions select { margin: 0; }
  .discipline-section { margin-bottom: 1.5rem; }
  .discipline-title { font-weight: 600; font-size: 0.95rem; margin-bottom: 0.4rem; text-transform: capitalize; }
  .badge-count { display: inline-block; background: #eee; border-radius: 10px; padding: 0 0.5rem; font-size: 0.75rem; margin-left: 0.4rem; }
  .revise-form { display: inline; }
  .revise-form button { margin: 0; padding: 0.15rem 0.5rem; font-size: 0.8rem; }
  .new-project { display: flex; gap: 0.75rem; align-items: flex-end; margin-bottom: 1.5rem; }
  .new-project label { flex: 1; margin-top: 0; }
  .new-project input, .new-project select { width: 100%; }
  .new-project button { margin: 0; }
"""

NAV = '<div class="nav"><a href="/">Projects</a><a href="/jurisdictions">Jurisdictions</a></div>'


PROJECTS_PAGE = """
<!doctype html>
<title>Projects - Plan Review Copilot</title>
<style>""" + BASE_STYLE + """</style>

""" + NAV + """
<h1>Projects</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}

{% if not jurisdictions %}
<p class="warn">No jurisdictions have reference documentation loaded yet -
  <a href="/jurisdictions">add one</a> before creating a project.</p>
{% endif %}

<form method="post" action="/projects" class="new-project">
  <label>New project name
    <input name="project_name" required>
  </label>
  <label>Jurisdiction
    <select name="jurisdiction_id" {% if not jurisdictions %}disabled{% endif %} required>
      {% for j in jurisdictions %}<option value="{{ j.id }}">{{ j.name }}</option>{% endfor %}
    </select>
  </label>
  <button type="submit">Create project</button>
</form>

<table>
  <tr><th>Project</th><th>Jurisdiction</th><th>Latest revision</th><th>Sheets</th><th>vs. code</th><th>cross-discipline</th></tr>
  {% for row in rows %}
  <tr>
    <td><a href="/projects/{{ row.id }}">{{ row.name }}</a></td>
    <td>{{ row.jurisdiction }}</td>
    <td>{{ row.revision }}</td>
    <td>{{ row.sheet_count }}</td>
    <td class="{{ row.jurisdiction_status_class }}">{{ row.jurisdiction_status }}</td>
    <td class="{{ row.cross_status_class }}">{{ row.cross_status }}</td>
  </tr>
  {% endfor %}
  {% if not rows %}
  <tr><td colspan="6" class="meta">No projects yet - create one above.</td></tr>
  {% endif %}
</table>
"""

PROJECT_PAGE = """
<!doctype html>
<title>{{ project.name }} - Plan Review Copilot</title>
<style>""" + BASE_STYLE + """</style>

""" + NAV + """
<p><a href="/">&larr; all projects</a></p>
<h1>{{ project.name }}</h1>
<p class="meta">
  Jurisdiction: {{ project.jurisdiction.name }}
  &middot; vs. code: <span class="{{ jurisdiction_status_class }}">{{ jurisdiction_status }}</span>
  &middot; cross-discipline: <span class="{{ cross_status_class }}">{{ cross_status }}</span>
</p>
{% if error %}<p class="error">{{ error }}</p>{% endif %}

{% if missing_disciplines %}
<p class="warn">No sheets yet for: {{ missing_disciplines|join(', ') }}</p>
{% endif %}

<div class="actions">
  <form method="post" action="/projects/{{ project.id }}/new-revision">
    <button type="submit">Start new revision</button>
  </form>
  <form method="post" action="/projects/{{ project.id }}/check">
    <label>Engine</label>
    <select name="engine">
      <option value="mock">mock (no API key)</option>
      <option value="groq">groq (free tier)</option>
      <option value="real">real (Claude, paid)</option>
    </select>
    <label title="Re-check every current clause from scratch, instead of only the ones never checked before">
      <input type="checkbox" name="force" value="1" style="width: auto; padding: 0;"> full re-check
    </label>
    <button type="submit">Check vs. code</button>
  </form>
  <form method="post" action="/projects/{{ project.id }}/check-cross-discipline">
    <label>Engine</label>
    <select name="engine">
      <option value="preview" title="Shows the semantic-similarity matches without judging conflict vs. no conflict - no API key needed">preview candidates (no LLM)</option>
      <option value="groq">groq (free tier)</option>
      <option value="real">real (Claude, paid)</option>
    </select>
    <label title="Re-check every current clause from scratch, instead of only the ones never checked before">
      <input type="checkbox" name="force" value="1" style="width: auto; padding: 0;"> full re-check
    </label>
    <button type="submit">Check cross-discipline</button>
  </form>
  <a href="/projects/{{ project.id }}/flags">View current flags &rarr;</a>
</div>

<h2>Current sheets{% if latest_submission %} <span class="meta">(as of {{ latest_submission.revision_label }} - each sheet shows its own most recent version)</span>{% endif %}</h2>
{% for discipline, rows in sheet_rows|groupby('discipline') %}
<div class="discipline-section">
  <div class="discipline-title">{{ discipline }} <span class="badge-count">{{ rows|length }}</span></div>
  <table>
    <tr><th>Sheet</th><th>Title</th><th>Type</th><th>File</th><th>Clauses</th><th>Submitted by</th><th>Ingested</th><th></th></tr>
    {% for row in rows %}
    <tr>
      <td class="label">{{ row.sheet }}</td>
      <td>{{ row.title }}</td>
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
</div>
{% endfor %}
{% if not sheet_rows %}
<p class="meta">No sheets ingested yet - add one below.</p>
{% endif %}

<h2>Add a sheet</h2>
{% if revising %}
<p class="info">Revising <strong>{{ prefill_sheet_number }}</strong> as a new revision - pick the updated file below.</p>
{% endif %}
<form method="post" action="/projects/{{ project.id }}/ingest" enctype="multipart/form-data">
  <fieldset>
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

<h2>Revision history</h2>
<table>
  <tr><th>Revision</th><th>Submitted</th><th></th></tr>
  {% for s, previous_seq in submission_rows %}
  <tr>
    <td>{{ s.revision_label }}</td>
    <td>{{ s.submitted_at.strftime('%Y-%m-%d %H:%M') }}</td>
    <td>
      {% if previous_seq %}
      <a href="/projects/{{ project.id }}/diff?from={{ previous_seq }}&to={{ s.sequence_number }}">view diff vs. previous</a>
      {% else %}
      <span class="meta">first revision</span>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>

<script>
  const PROJECT_ID = "{{ project.id }}";
  async function autofillFromExistingSheet() {
    const sheet = document.getElementById('sheet_number').value.trim();
    const titleInput = document.getElementById('title');
    const disciplineSelect = document.getElementById('discipline');
    titleInput.classList.remove('autofilled');
    disciplineSelect.classList.remove('autofilled');
    if (!sheet) return;

    const res = await fetch(`/series-info?project_id=${encodeURIComponent(PROJECT_ID)}&sheet_number=${encodeURIComponent(sheet)}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data) return;

    titleInput.value = data.title;
    disciplineSelect.value = data.discipline;
    titleInput.classList.add('autofilled');
    disciplineSelect.classList.add('autofilled');
  }
  document.getElementById('sheet_number').addEventListener('blur', autofillFromExistingSheet);
</script>
"""

JURISDICTIONS_PAGE = """
<!doctype html>
<title>Jurisdictions - Plan Review Copilot</title>
<style>""" + BASE_STYLE + """</style>

""" + NAV + """
<h1>Jurisdictions</h1>
<p class="meta">Only jurisdictions listed here (with at least one document loaded) are selectable when creating a project.</p>
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
<style>""" + BASE_STYLE + """</style>

<p><a href="{{ back_href }}">&larr; back</a></p>
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

FLAGS_PAGE = """
<!doctype html>
<title>Flags - {{ project }} - Plan Review Copilot</title>
<style>""" + BASE_STYLE + """</style>

<p><a href="/projects/{{ project_id }}">&larr; back to {{ project }}</a></p>
<h1>Current flags &mdash; {{ project }}</h1>
<p class="info">Every clause is only ever reasoned about once per check type - a flag
  found under an earlier revision stays current here until the clause it's about is
  itself superseded by a later revision, not until the whole project is re-checked.</p>
<p class="meta">{{ flags|length }} flag(s) found &middot; <a href="/projects/{{ project_id }}/audit-report">view audit report</a></p>

<table>
  <tr><th>Type</th><th>Severity</th><th>Clause</th><th>Found in</th><th>Explanation</th><th>Cited</th><th>Model</th><th></th></tr>
  {% for f in flags %}
  <tr>
    <td>{{ "cross-discipline" if f.check_type.value == "cross_discipline" else "vs. code" }}</td>
    <td class="sev-{{ f.severity.value }}">{{ f.severity.value }}</td>
    <td class="label">{{ f.clause.clause_label }}</td>
    <td>{{ f.submission.revision_label }}</td>
    <td>{{ f.explanation }}</td>
    <td>
      {% for c in f.citations %}
        {% if c.jurisdiction_clause %}<div>{{ c.jurisdiction_clause.clause_label }}</div>{% endif %}
        {% if c.clause %}<div>{{ c.clause.series.discipline.value }} {{ c.clause.clause_label }}</div>{% endif %}
      {% endfor %}
    </td>
    <td>
      {{ f.model }}
      {% if f.is_simulated %}<span class="placeholder-badge" title="Keyword heuristic, not real model reasoning">simulated</span>{% endif %}
    </td>
    <td><a href="/flags/{{ f.id }}/reasoning">view reasoning</a></td>
  </tr>
  {% endfor %}
</table>
"""

REASONING_PAGE = """
<!doctype html>
<title>Reasoning - {{ flag.clause.clause_label }} - Plan Review Copilot</title>
<style>""" + BASE_STYLE + """
  .meta div { margin-bottom: 0.2rem; }
  pre {
    background: #f6f6f6; border: 1px solid #ddd; border-radius: 3px; padding: 0.8rem;
    white-space: pre-wrap; word-break: break-word; font-size: 0.85rem;
  }
</style>

<p><a href="/submissions/{{ flag.submission_id }}/flags">&larr; back to flags</a></p>
<h1>Reasoning trace</h1>

<div class="meta">
  <div>Clause: <span class="label">{{ flag.clause.clause_label }}</span></div>
  <div>Severity: <span class="sev-{{ flag.severity.value }}">{{ flag.severity.value }}</span></div>
  <div>Engine: {{ llm_call.engine }} / {{ llm_call.model }}</div>
  <div>Tokens: {{ llm_call.input_tokens if llm_call.input_tokens is not none else '-' }} in
       / {{ llm_call.output_tokens if llm_call.output_tokens is not none else '-' }} out</div>
  <div>Latency: {{ llm_call.latency_ms }} ms</div>
  <div>Called: {{ llm_call.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</div>
</div>

<h2>Flag explanation</h2>
<pre>{{ flag.explanation }}</pre>

<h2>Prompt sent to model</h2>
<pre>{{ llm_call.prompt }}</pre>

<h2>Raw model response</h2>
<pre>{{ llm_call.raw_response }}</pre>
"""

CROSS_DISCIPLINE_CANDIDATES_PAGE = """
<!doctype html>
<title>Cross-discipline candidates - {{ project.name }} - Plan Review Copilot</title>
<style>""" + BASE_STYLE + """
  .score { font-variant-numeric: tabular-nums; color: #444; }
</style>

<p><a href="/projects/{{ project.id }}">&larr; back to {{ project.name }}</a></p>
<h1>Cross-discipline candidates &mdash; {{ project.name }} {{ revision }}</h1>
<p class="info">Retrieval only - no LLM was called. This is the semantic-similarity
  matching step that narrows candidates before a reasoning engine judges whether
  a real conflict exists; it does not itself determine conflict vs. no conflict.</p>

<table>
  <tr><th>Clause</th><th>Candidate (other discipline)</th><th>Similarity</th></tr>
  {% for clause, candidates in rows %}
    {% for candidate, score in candidates %}
    <tr>
      <td class="label">{{ clause.series.discipline.value }} {{ clause.clause_label }}: {{ clause.text[:80] }}</td>
      <td class="label">{{ candidate.series.discipline.value }} {{ candidate.clause_label }}: {{ candidate.text[:80] }}</td>
      <td class="score">{{ "%.2f"|format(score) }}</td>
    </tr>
    {% else %}
    <tr>
      <td class="label">{{ clause.series.discipline.value }} {{ clause.clause_label }}</td>
      <td colspan="2">(no candidates above similarity threshold)</td>
    </tr>
    {% endfor %}
  {% endfor %}
</table>
"""

DIFF_PAGE = """
<!doctype html>
<title>Diff {{ from_revision }} → {{ to_revision }} - {{ project.name }} - Plan Review Copilot</title>
<style>""" + BASE_STYLE + """
  .added { background: #eaf7ea; }
  .removed { background: #fbeaea; text-decoration: line-through; color: #7a3030; }
  .diff-tag { font-size: 0.7rem; text-transform: uppercase; font-weight: 600; padding: 0.05rem 0.4rem; border-radius: 3px; margin-right: 0.4rem; }
  .diff-tag-new { background: #d6ecd6; color: #1a5c1a; }
  .diff-tag-removed { background: #f3d6d6; color: #7a3030; }
</style>

<p><a href="/projects/{{ project.id }}">&larr; back to {{ project.name }}</a></p>
<h1>Diff: {{ from_revision }} &rarr; {{ to_revision }}</h1>
<p class="meta">{{ project.name }} &middot; sheets with no change between these two revisions are omitted</p>

{% if not sheets %}
<p class="info">No clause-level changes between these two revisions.</p>
{% endif %}

{% for sheet in sheets %}
<div class="discipline-section">
  <div class="discipline-title">
    {% if sheet.is_new_sheet %}<span class="diff-tag diff-tag-new">new sheet</span>{% endif %}
    {% if sheet.is_removed_sheet %}<span class="diff-tag diff-tag-removed">removed</span>{% endif %}
    {{ sheet.series.discipline.value }} {{ sheet.series.sheet_number }} &mdash; {{ sheet.series.title }}
    <span class="badge-count">{{ sheet.unchanged_count }} unchanged</span>
  </div>
  <table>
    <tr><th>Change</th><th>Label</th><th>Text</th></tr>
    {% for c in sheet.added %}
    <tr class="added">
      <td><span class="diff-tag diff-tag-new">added</span></td>
      <td class="label">{{ c.clause_label }}</td>
      <td>{{ c.text }}</td>
    </tr>
    {% endfor %}
    {% for c in sheet.removed %}
    <tr class="removed">
      <td><span class="diff-tag diff-tag-removed">removed</span></td>
      <td class="label">{{ c.clause_label }}</td>
      <td>{{ c.text }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endfor %}
"""

AUDIT_REPORT_PAGE = """
<!doctype html>
<title>Audit report - {{ project.name }} - Plan Review Copilot</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  .no-print { margin-bottom: 1.5rem; }
  .no-print a, .no-print button { margin-right: 0.75rem; }
  header { border-bottom: 3px solid #1a1a1a; padding-bottom: 1rem; margin-bottom: 1.5rem; }
  header h1 { font-size: 1.5rem; margin: 0 0 0.2rem 0; }
  header .subtitle { color: #555; font-size: 0.9rem; }
  .summary { display: flex; gap: 1.5rem; margin-bottom: 2rem; flex-wrap: wrap; }
  .summary-card { border: 1px solid #ccc; border-radius: 4px; padding: 0.6rem 1rem; min-width: 120px; }
  .summary-card .n { font-size: 1.4rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .summary-card .label { font-size: 0.75rem; color: #555; text-transform: uppercase; letter-spacing: 0.03em; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; font-size: 0.82rem; vertical-align: top; }
  th { border-bottom: 2px solid #999; }
  .label { font-family: ui-monospace, monospace; white-space: nowrap; }
  .sev-high { color: #b00020; font-weight: 600; }
  .sev-medium { color: #a15c00; font-weight: 600; }
  .sev-low { color: #555; }
  footer { color: #888; font-size: 0.75rem; margin-top: 2rem; border-top: 1px solid #ddd; padding-top: 0.75rem; }
  @media print {
    .no-print { display: none; }
    body { margin: 0; max-width: none; }
    a { color: inherit; text-decoration: none; }
  }
</style>

<div class="no-print">
  <a href="/projects/{{ project.id }}">&larr; back to {{ project.name }}</a>
  <a href="/projects/{{ project.id }}/audit-report.csv">Download CSV</a>
  <button onclick="window.print()">Print / Save as PDF</button>
</div>

<header>
  <h1>Conflict Detection Audit Report</h1>
  <div class="subtitle">
    {{ project.name }} &middot; Jurisdiction: {{ project.jurisdiction.name }} &middot; Generated {{ generated_at }}
  </div>
</header>

<div class="summary">
  <div class="summary-card"><div class="n">{{ flags|length }}</div><div class="label">Total flags</div></div>
  <div class="summary-card"><div class="n">{{ high_count }}</div><div class="label">High severity</div></div>
  <div class="summary-card"><div class="n">{{ medium_count }}</div><div class="label">Medium severity</div></div>
  <div class="summary-card"><div class="n">{{ low_count }}</div><div class="label">Low severity</div></div>
  <div class="summary-card"><div class="n">{{ sheet_count }}</div><div class="label">Sheets covered</div></div>
</div>

<table>
  <tr><th>Type</th><th>Severity</th><th>Sheet / Clause</th><th>Finding</th><th>Cited</th><th>Model</th><th>Revision</th><th>Timestamp</th></tr>
  {% for f in flags %}
  <tr>
    <td>{{ "cross-discipline" if f.check_type.value == "cross_discipline" else "vs. code" }}</td>
    <td class="sev-{{ f.severity.value }}">{{ f.severity.value }}</td>
    <td class="label">{{ f.clause.series.discipline.value }} {{ f.clause.series.sheet_number }} / {{ f.clause.clause_label }}</td>
    <td>
      {{ f.explanation }}
      {% if f.is_simulated %}<br><em>(simulated - keyword heuristic, not model reasoning)</em>{% endif %}
    </td>
    <td>
      {% for c in f.citations %}
        {% if c.jurisdiction_clause %}<div>{{ c.jurisdiction_clause.clause_label }}</div>{% endif %}
        {% if c.clause %}<div>{{ c.clause.series.discipline.value }} {{ c.clause.clause_label }}</div>{% endif %}
      {% endfor %}
    </td>
    <td>{{ f.model }}</td>
    <td>{{ f.submission.revision_label }}</td>
    <td>{{ f.llm_call.created_at.strftime('%Y-%m-%d %H:%M') if f.llm_call else '-' }}</td>
  </tr>
  {% endfor %}
  {% if not flags %}
  <tr><td colspan="8">No flags found - project is clean as of this report.</td></tr>
  {% endif %}
</table>

<footer>
  Generated by Plan Review Copilot. Reflects the project's current flag set as of {{ generated_at }} -
  a "current" flag is one whose clause is still part of the project's live document set; superseded
  clauses' historical flags are retained in the system but excluded from this report. Full reasoning
  traces (prompt, raw model response, token usage) for any flag are available in the application.
</footer>
"""


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


@app.get("/")
def index():
    session = get_session()
    return render_template_string(
        PROJECTS_PAGE,
        jurisdictions=jurisdictions_with_documentation(session),
        rows=_project_summary_rows(session),
        error=request.args.get("error"),
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

    return render_template_string(
        PROJECT_PAGE,
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

    submission = get_latest_or_create_submission(session, project.id)
    engine = request.form.get("engine", "mock")
    force = request.form.get("force") == "1"
    try:
        check_submission_for_conflicts(session, submission, engine=engine, force=force)
    except Exception as e:
        session.rollback()
        # Some exceptions (e.g. SQLAlchemy's, which embed the raw SQL) span
        # multiple lines - collapse to one line, since a redirect target is a
        # URL, not a place to render a full stack of SQL.
        message = " ".join(str(e).split())
        return redirect(f"/projects/{project_id}?{urlencode({'error': f'Check failed: {message}'})}")
    session.commit()
    return redirect(f"/projects/{project_id}/flags")


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

    submission = get_latest_or_create_submission(session, project.id)
    force = request.form.get("force") == "1"
    try:
        check_submission_for_cross_discipline_conflicts(session, submission, engine=engine, force=force)
    except Exception as e:
        session.rollback()
        message = " ".join(str(e).split())
        return redirect(f"/projects/{project_id}?{urlencode({'error': f'Cross-discipline check failed: {message}'})}")
    session.commit()
    return redirect(f"/projects/{project_id}/flags")


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

    return render_template_string(
        CROSS_DISCIPLINE_CANDIDATES_PAGE,
        project=project,
        revision=submission.revision_label,
        rows=rows,
    )


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

    return render_template_string(
        DIFF_PAGE,
        project=project,
        from_revision=from_submission.revision_label,
        to_revision=to_submission.revision_label,
        sheets=sheets,
    )


def _severity_rank_sort(flags):
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(flags, key=lambda f: severity_rank[f.severity.value])


@app.get("/projects/<project_id>/audit-report")
def project_audit_report(project_id):
    session = get_session()
    project = session.get(Project, project_id)
    if project is None:
        return redirect("/?error=Project not found")

    flags = _severity_rank_sort(current_flags_for_project(session, project_id))
    sheet_count = session.query(DocumentSeries).filter_by(project_id=project_id).count()

    return render_template_string(
        AUDIT_REPORT_PAGE,
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

    flags = _severity_rank_sort(current_flags_for_project(session, project_id))
    return render_template_string(
        FLAGS_PAGE,
        project=project.name,
        project_id=project.id,
        flags=flags,
    )


@app.get("/flags/<flag_id>/reasoning")
def flag_reasoning(flag_id):
    session = get_session()
    flag = session.get(Flag, flag_id)
    if flag is None:
        return redirect("/?error=Flag not found")
    return render_template_string(REASONING_PAGE, flag=flag, llm_call=flag.llm_call)


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
        return redirect(f"/jurisdictions?{urlencode({'error': str(e)})}")

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
        back_href="/jurisdictions",
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
        back_href=f"/projects/{document.series.project_id}",
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
