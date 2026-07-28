// Shared inline document viewer modal - see app/templates/_viewer_modal.html
// for the markup this drives. Used from both project_detail.html (sheet-table
// "View" links) and flags.html ("locate" links), so it lives in one file
// rather than being duplicated per template.
//
// Renders each PDF page as a lazy-loaded <img> (server rasterizes via
// PyMuPDF, see /documents/<id>/pages/<n>.png) rather than paging through
// fixed-size windows - a real browser's native loading="lazy" already avoids
// fetching offscreen pages. document/page/clause are reflected into the URL
// on project_detail.html so a flag's "locate" link can deep-link straight to
// a specific page/clause (see the DOMContentLoaded handler at the bottom).

function openViewerModal() {
  document.getElementById('viewer-overlay').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeViewerModal() {
  document.getElementById('viewer-overlay').hidden = true;
  document.body.style.overflow = '';
  clearClauseHighlight();
  const params = new URLSearchParams(window.location.search);
  params.delete('document');
  params.delete('page');
  params.delete('clause');
  const query = params.toString();
  history.replaceState(null, '', query ? `?${query}` : window.location.pathname);
}

async function loadViewerDocument(documentId, page) {
  const pagesEl = document.getElementById('viewer-pages');
  pagesEl.innerHTML = '<p class="meta">Loading&hellip;</p>';
  pagesEl.classList.remove('viewer-pages--text');

  const countRes = await fetch(`/documents/${documentId}/page-count`);
  const countData = await countRes.json();

  if (countData.page_count) {
    pagesEl.innerHTML = '';
    for (let i = 1; i <= countData.page_count; i++) {
      const wrap = document.createElement('div');
      wrap.className = 'viewer-page-wrap';
      wrap.id = `viewer-page-wrap-${i}`;
      const img = document.createElement('img');
      img.src = `/documents/${documentId}/pages/${i}.png`;
      img.loading = 'lazy';
      img.className = 'viewer-page';
      img.alt = `Page ${i}`;
      wrap.appendChild(img);
      pagesEl.appendChild(wrap);
    }
    if (page) {
      const target = document.getElementById(`viewer-page-wrap-${page}`);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    return;
  }

  // Not a rasterizable PDF - fall back to the plain-text extraction path
  // (.txt/.docx only; see _document_has_preview's docstring for what's
  // excluded and why).
  const textRes = await fetch(`/documents/${documentId}/text`);
  const textData = await textRes.json();
  if (textData.text) {
    pagesEl.innerHTML = '';
    pagesEl.classList.add('viewer-pages--text');
    const pre = document.createElement('pre');
    pre.className = 'viewer-text mono';
    pre.textContent = textData.text;
    pagesEl.appendChild(pre);
    return;
  }

  pagesEl.innerHTML = '<p class="meta">No preview available for this file type - use the download link instead.</p>';
}

async function viewDocument(event, documentId, page) {
  if (event) event.preventDefault();
  openViewerModal();
  document.getElementById('viewer-doc-select').value = documentId;
  await loadViewerDocument(documentId, page);
  resetClausePicker();
  await loadClauseOptions();

  // Fresh params, not extended from the current URL - opening a document
  // this way (sheet-table "View" link, or the dropdown) starts a new viewer
  // state, so any clause highlight left over from a previous ?clause=
  // deep-link shouldn't silently carry over onto an unrelated page. Only
  // meaningful on project_detail.html (flags.html doesn't read these back on
  // load), but harmless to set either way.
  const params = new URLSearchParams();
  params.set('document', documentId);
  if (page) params.set('page', page);
  history.replaceState(null, '', `?${params.toString()}`);
}

// Doc-selector dropdown's onchange - distinct from viewDocument() because
// that one also drives the initial "View" link/deep-link open (where the
// caller already knows the target page); switching documents by hand has no
// page in mind yet, so it just opens at the top.
function onViewerDocChange(documentId) {
  viewDocument(null, documentId, null);
}

// "Jump to clause" dropdown, scoped to whichever document is currently open
// - free-text search filters server-side (same label/text match as the full
// clause table, see document_clause_options()), debounced so typing doesn't
// fire a request per keystroke.
let clauseSearchDebounce = null;
function onClauseSearchInput() {
  clearTimeout(clauseSearchDebounce);
  clauseSearchDebounce = setTimeout(loadClauseOptions, 200);
}

function resetClausePicker() {
  document.getElementById('viewer-clause-search').value = '';
  document.getElementById('viewer-clause-picker').innerHTML = '<option value="">Jump to clause&hellip;</option>';
}

async function loadClauseOptions() {
  const documentId = document.getElementById('viewer-doc-select').value;
  const picker = document.getElementById('viewer-clause-picker');
  if (!documentId) return;

  const q = document.getElementById('viewer-clause-search').value.trim();
  const res = await fetch(`/documents/${documentId}/clause-options?${new URLSearchParams({ q })}`);
  const clauses = await res.json();

  picker.innerHTML = '<option value="">Jump to clause&hellip;</option>';
  for (const c of clauses) {
    const option = document.createElement('option');
    option.value = c.id;
    option.textContent = `${c.label} — ${c.preview}`;
    picker.appendChild(option);
  }
}

function onClausePicked(clauseId) {
  if (!clauseId) return;
  applyClauseHighlight(clauseId);
}

// Locate-in-document: a flag's "locate" link (see flags.html) opens the
// modal directly via this, without navigating away first. Fetches the
// clause's info itself (rather than reusing applyClauseHighlight, which
// also writes ?clause= into the URL - only meaningful on project_detail.html)
// so flags.html's own URL stays untouched.
async function openViewerModalForClause(event, clauseId) {
  if (event) event.preventDefault();
  const res = await fetch(`/clauses/${clauseId}/info`);
  if (!res.ok) return;
  const info = await res.json();
  if (!info.document_id) {
    openViewerModal();
    document.getElementById('viewer-pages').innerHTML =
      '<p class="meta">That clause\'s sheet isn\'t part of the current revision anymore.</p>';
    return;
  }
  await viewDocument(null, info.document_id, info.page);
  highlightClauseInfo(info);
}

// Fetches a clause's info (see /clauses/<id>/info) and highlights it -
// used by the "jump to clause" picker and project_detail.html's own
// ?clause= deep-link, both of which want the URL kept in sync.
async function applyClauseHighlight(clauseId) {
  const res = await fetch(`/clauses/${clauseId}/info`);
  if (!res.ok) return;
  const info = await res.json();

  const params = new URLSearchParams(window.location.search);
  params.set('clause', clauseId);
  history.replaceState(null, '', `?${params.toString()}`);

  highlightClauseInfo(info);
}

// Pure visual highlight application, given an already-fetched clause info
// object - a real inline <mark> for text-mode SPEC docs (exact match, since
// we have the canonical clause text to search for), a drawn box for PDFs
// where a bbox was found (see /clauses/<id>/info and _find_clause_bbox's
// docstring), or an outlined page as a fallback when it wasn't
// (ambiguous/drifted anchor - Clause.location only stores {page}, no bbox,
// for older extractions or a search that didn't resolve to one match).
function highlightClauseInfo(info) {
  clearClauseHighlight();
  showLocateBanner(info.clause_label);

  const textEl = document.querySelector('.viewer-text');
  if (textEl) {
    highlightTextClause(textEl, info.text);
    return;
  }

  if (!info.page) return;
  const wrap = document.getElementById(`viewer-page-wrap-${info.page}`);
  if (!wrap) return;

  if (info.bbox) {
    const drawBox = () => {
      const box = document.createElement('div');
      box.className = 'clause-bbox-highlight';
      box.id = 'clause-bbox-active';
      box.style.left = `${info.bbox[0] * 100}%`;
      box.style.top = `${info.bbox[1] * 100}%`;
      box.style.width = `${(info.bbox[2] - info.bbox[0]) * 100}%`;
      box.style.height = `${(info.bbox[3] - info.bbox[1]) * 100}%`;
      wrap.appendChild(box);
      wrap.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    const img = wrap.querySelector('img');
    if (img.complete) drawBox(); else img.addEventListener('load', drawBox, { once: true });
  } else {
    wrap.classList.add('viewer-page--highlighted');
    wrap.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function highlightTextClause(container, clauseText) {
  const needle = clauseText.trim();
  const full = container.textContent;
  const idx = full.indexOf(needle);
  if (idx === -1) return;  // extraction/edit drift - clause text no longer matches verbatim, skip rather than mis-highlight

  container.innerHTML = '';
  container.appendChild(document.createTextNode(full.slice(0, idx)));
  const mark = document.createElement('mark');
  mark.className = 'clause-highlight';
  mark.id = 'clause-highlight-active';
  mark.textContent = full.slice(idx, idx + needle.length);
  container.appendChild(mark);
  container.appendChild(document.createTextNode(full.slice(idx + needle.length)));
  mark.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function showLocateBanner(clauseLabel) {
  let banner = document.getElementById('viewer-locate-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'viewer-locate-banner';
    banner.className = 'viewer-locate-banner';
    document.getElementById('viewer-pages').insertAdjacentElement('beforebegin', banner);
  }
  banner.innerHTML = '';
  const label = document.createElement('span');
  label.textContent = `📍 Locating ${clauseLabel}`;
  banner.appendChild(label);
  const clearBtn = document.createElement('button');
  clearBtn.type = 'button';
  clearBtn.className = 'btn ghost sm';
  clearBtn.textContent = 'Clear';
  clearBtn.onclick = clearClauseHighlight;
  banner.appendChild(clearBtn);
}

function clearClauseHighlight() {
  const banner = document.getElementById('viewer-locate-banner');
  if (banner) banner.remove();
  document.querySelectorAll('.viewer-page--highlighted').forEach((el) => el.classList.remove('viewer-page--highlighted'));
  const box = document.getElementById('clause-bbox-active');
  if (box) box.remove();
  const mark = document.getElementById('clause-highlight-active');
  if (mark) {
    const parent = mark.parentNode;
    parent.replaceChild(document.createTextNode(mark.textContent), mark);
    parent.normalize();
  }
  const params = new URLSearchParams(window.location.search);
  params.delete('clause');
  history.replaceState(null, '', params.toString() ? `?${params.toString()}` : window.location.pathname);
}

// project_detail.html-only deep-link entry: ?document=&page=&clause= opens
// the modal straight to the right spot (this is what a flags-page "locate"
// link used to do via a full page redirect, before it became a same-page
// modal - kept for anyone with an old link, or a bookmarked URL).
document.addEventListener('DOMContentLoaded', () => {
  if (!document.getElementById('viewer-overlay')) return;
  const params = new URLSearchParams(window.location.search);
  const documentId = params.get('document');
  const clauseId = params.get('clause');
  if (documentId) {
    viewDocument(null, documentId, params.get('page')).then(() => {
      if (clauseId) applyClauseHighlight(clauseId);
    });
  }
});
