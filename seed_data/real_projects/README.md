# Real-document pipeline test set

Five sheets pulled from a real, public multi-discipline construction bid set
(UCCS Cybersecurity and Space Ecosystem Expansion, 2021 - see
`manifest.json` for source URL), covering architectural, fire protection,
plumbing, and mechanical, with a mix of prose-note pages and legend/symbol
pages. Load with:

    .venv/bin/python scripts/seed_real_projects.py

This is the **pipeline stress-test track**, distinct from
`seed_data/synthetic_demo` (the labeled conflict track). No jurisdiction
documentation is loaded for "Colorado Springs, CO" - don't expect meaningful
conflict flags here, the point is exercising extraction against real,
messy, CAD-exported drawing sheets.

## Known finding: clause extraction is currently poor on real drawing sheets

Ingesting these 5 sheets and running `extract_and_store_clauses` on a scratch
DB surfaced a real, un-fixed gap (as of 2026-07-19, not yet addressed):

- On the one page that's genuinely dense prose (`G002`, fire & life safety
  general notes), extraction produced 30 clauses, but many labels are stray
  numbers pulled from tables/schedules ("70", "75", "0") rather than real
  section markers - similar in kind to the table-noise problem already fixed
  for jurisdiction PDFs, but not yet addressed for project-side sheets.
- On every other page (`A002`, `FP-notes`, `P-notes`, `M-legend`), the
  extractor produced almost entirely **title-block noise**: addresses, phone
  numbers, and revision-stamp dates ("12499 WEST COLFAX AVENUE...",
  "720-213-7550", "19OCT2020"). The real numbered scope-of-work text visible
  on these pages (e.g. `FP-notes`' "1. SCOPE OF WORK: PROVIDE A COMPLETE
  HYDRAULICALLY-CALCULATED...") did not survive as a clause at all.

Root cause (not yet fixed): PyMuPDF's `get_text()` reading order follows the
underlying PDF content stream, not visual layout. On a linear code-book PDF
that's fine - paragraphs come out in order. On a CAD-exported sheet, the
title-block sidebar (firm addresses, phone numbers, revision history) is
interleaved with the body notes in the content stream, so `CLAUSE_MARKER`'s
line-start regex fires on stray numbers from the sidebar as often as on real
numbered notes, and the fallback text-quality filter (`MIN_WORDS`, see
`app/clause_extraction.py`) isn't strict enough to tell "12499 WEST COLFAX
AVENUE" from a real short clause like "4.2 Second note."

This is a real, quantified gap worth fixing before treating project-side
extraction as demo-ready, separate from (and in addition to) the "ingest
non-text plans" roadmap item - these are text-extractable pages that still
extract badly, not scanned/BIM files.
