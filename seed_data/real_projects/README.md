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

## Known finding: clause extraction quality on real drawing sheets

Ingesting these 5 sheets and running `extract_and_store_clauses` originally
(as of 2026-07-19) surfaced a real gap: on 4 of 5 pages, extraction produced
almost entirely **title-block noise** (addresses, phone numbers, revision
dates) instead of the real numbered notes. Two fixes landed in
`app/clause_extraction.py::extract_pages` on 2026-07-21, verified against
these exact 5 files:

- **Title-block sidebar redaction.** CAD-exported sheets carry a title-block
  sidebar (firm addresses, phone numbers, revision history, sheet/project
  number) in a narrow column along the sheet's right edge.
  `page.get_text()`'s reading order follows the PDF content stream, not
  visual layout, so that sidebar text interleaves with real body notes -
  `CLAUSE_MARKER`'s line-start regex then fires on a phone number
  ("720-213-7550" reads identically to a hierarchical section number) or a
  street address ("12499 WEST COLFAX AVENUE" clears the `MIN_WORDS` filter)
  as readily as a real numbered note. Verified via `get_text("blocks")`
  bounding boxes that every one of these 5 sheets has a hard gap with zero
  content between 71% and 91% of page width - real content never crosses
  71%, title-block content never starts before 91% - so redacting the
  rightmost margin (`RIGHT_MARGIN_CUTOFF`) is safe across the set.
- **False-positive whole-page "table" detection.** `find_tables()` was
  misreading dense, grid-aligned legend/schedule content as one giant sparse
  table spanning ~84-86% of the page area on every one of these 5 sheets -
  large enough to engulf real prose blocks in its bounding box and redact
  them along with actual noise. Every genuine detected table on the same
  pages stayed under ~2% of page area, so tables above 30% of page area are
  now skipped rather than redacted.

**Result, verified against these exact files:**
- `FP-notes` and `P-notes`: fixed. Real numbered notes ("1. SCOPE OF WORK:
  PROVIDE A COMPLETE HYDRAULICALLY-CALCULATED...", "1. INSTALL PLUMBING IN
  ACCORDANCE WITH...") now extract correctly with zero title-block noise.
- `M-legend`: mostly fixed - the real numbered general notes on this sheet
  now extract correctly, though the legend/abbreviation-table portion of the
  same page still chunks messily (see below).
- `A002` and `G002`: still poor, for a different reason than the original
  finding. `A002` is a symbol/abbreviation legend, not prose - the
  numbered-clause heuristic was never going to suit that content shape
  regardless of noise filtering. `G002` is a harder case: its title-block
  content sits at ~69-70% of page width, not the ~91%+ seen on the other 4
  sheets, overlapping the same column as genuine body prose on that specific
  page - a position-only cutoff can't separate them there. Confirmed by
  checking `get_text("blocks")` directly, not assumed.

So: this is real, verified progress on the sheets that follow the majority
template, not a full fix. A pure position/area heuristic has a real ceiling
on real-world CAD templates - the residual `G002`/legend-page cases would
need per-span content classification (or a differently-shaped page layout
model) to close, not another positional tweak.
