# Document/Clause Storage — Entity Relationship Diagram

Reflects [app/models.py](../app/models.py). Source of truth is the code —
regenerate this by hand if the models change. See [WORKFLOW.md](WORKFLOW.md)
for the *process* this schema supports (ingest → retrieve → reason → trace →
continuous re-check), not just the entities.

```mermaid
erDiagram
    JURISDICTION {
        string id PK
        string name UK
        datetime created_at
    }

    JURISDICTION_DOCUMENT {
        string id PK
        string jurisdiction_id FK
        string title
        string doc_type "bim | pdf_2d | spec | municode_scrape"
        string file_uri
        string file_hash
        json metadata "nullable - freshness_source_id links a municode_scrape doc back to its FRESHNESS_SOURCE"
        datetime ingested_at
    }

    JURISDICTION_CLAUSE {
        string id PK "jurisdiction_clause_id cited by the LLM"
        string jurisdiction_document_id FK
        string clause_label
        string text
        string content_hash UK "unique per jurisdiction_document"
        string extraction_method "pdf_text | spec_text | ifc_property | manual | municode_scrape"
        json location
        datetime created_at
    }

    PROJECT {
        string id PK
        string name
        string jurisdiction_id FK
        datetime created_at
    }

    SUBMISSION {
        string id PK
        string project_id FK
        string revision_label
        int sequence_number UK "unique per project"
        datetime submitted_at
        string submitted_by
        string status "processing | reviewed | superseded"
        datetime created_at
    }

    DOCUMENT_SERIES {
        string id PK
        string project_id FK
        string discipline "architectural | structural | mechanical | electrical | plumbing | civil | fire_protection | low_voltage"
        string sheet_number UK "unique per project"
        string title
    }

    DOCUMENT {
        string id PK
        string document_series_id FK
        string submission_id FK
        string doc_type "bim | pdf_2d | spec"
        string file_uri
        string file_hash
        json metadata "nullable"
        datetime ingested_at
    }

    CLAUSE {
        string id PK "clause_id cited by the LLM"
        string document_series_id FK
        string clause_label
        string text
        string content_hash UK "unique per document_series; drives dedup"
        string extraction_method "pdf_text | spec_text | ifc_property | manual"
        json location "page/bbox for 2D, ifc_guid/level/grid for BIM"
        string first_seen_document_id FK
        datetime created_at
    }

    DOCUMENT_CLAUSE {
        string document_id PK
        string clause_id PK
    }

    LLM_CALL {
        string id PK
        string submission_id FK
        string clause_id FK
        string check_type "jurisdiction | cross_discipline"
        string engine "mock | groq | real"
        string model
        string prompt
        string raw_response
        int input_tokens "nullable"
        int output_tokens "nullable"
        int latency_ms
        datetime created_at
    }

    FLAG {
        string id PK
        string submission_id FK
        string clause_id FK
        string llm_call_id FK
        string check_type "jurisdiction | cross_discipline"
        string severity "low | medium | high"
        string explanation
        string model
        bool is_simulated
        datetime created_at
        string status "open | acknowledged | resolved | false_positive"
        string status_note "nullable"
        datetime status_updated_at "nullable"
    }

    FLAG_CITATION {
        string id PK
        string flag_id FK
        string clause_id FK "nullable - project-clause citation"
        string jurisdiction_clause_id FK "nullable - jurisdiction-clause citation"
    }

    FRESHNESS_SOURCE {
        string id PK
        string kind "municode | icc_public"
        string label
        string jurisdiction_id FK "nullable - null for icc_public, which isn't jurisdiction-specific"
        string source_ref "municode: 'productId:nodeId' | icc_public: sitemap URL"
        int check_interval_seconds
        datetime created_at
    }

    FRESHNESS_SNAPSHOT {
        string id PK
        string source_id FK
        datetime fetched_at
        string content_hash "sha256 of normalized content, not raw bytes"
        string raw_content_uri "content-addressable, same LocalFileStorage as documents"
    }

    FRESHNESS_CHANGE {
        string id PK
        string source_id FK
        datetime detected_at
        string prev_snapshot_id FK "nullable - null on a source's first-ever snapshot"
        string new_snapshot_id FK
        string summary "human-readable - which section/edition-year changed"
    }

    JURISDICTION ||--o{ JURISDICTION_DOCUMENT : "has reference docs"
    JURISDICTION_DOCUMENT ||--o{ JURISDICTION_CLAUSE : "owns"
    JURISDICTION ||--o{ PROJECT : "governs"
    JURISDICTION |o--o{ FRESHNESS_SOURCE : "optionally monitored by"
    FRESHNESS_SOURCE ||--o{ FRESHNESS_SNAPSHOT : "fetched into"
    FRESHNESS_SOURCE ||--o{ FRESHNESS_CHANGE : "detected on"

    PROJECT ||--o{ SUBMISSION : "has revisions"
    PROJECT ||--o{ DOCUMENT_SERIES : "has sheets/specs"
    SUBMISSION ||--o{ DOCUMENT : "contains versions"
    DOCUMENT_SERIES ||--o{ DOCUMENT : "has versions"
    DOCUMENT_SERIES ||--o{ CLAUSE : "owns"
    DOCUMENT ||--o{ DOCUMENT_CLAUSE : "includes"
    CLAUSE ||--o{ DOCUMENT_CLAUSE : "appears in"
    DOCUMENT ||--o| CLAUSE : "first_seen_document_id"

    SUBMISSION ||--o{ LLM_CALL : "audited by"
    CLAUSE ||--o{ LLM_CALL : "reasoned over in"
    LLM_CALL ||--o{ FLAG : "produced"
    SUBMISSION ||--o{ FLAG : "raised against"
    CLAUSE ||--o{ FLAG : "about"
    FLAG ||--o{ FLAG_CITATION : "supported by"
    CLAUSE ||--o{ FLAG_CITATION : "cited as (project clause)"
    JURISDICTION_CLAUSE ||--o{ FLAG_CITATION : "cited as (code clause)"
```

## Reading this diagram

- **`PROJECT → SUBMISSION`**: one project has many revision events ("Rev A",
  "Rev B", ...).
- **`DOCUMENT_SERIES`**: the logical identity of a sheet (e.g. "S-201") that
  persists across revisions. `DOCUMENT` rows are the actual versioned files;
  `DOCUMENT_SERIES` is what lets you ask "give me every version of S-201."
- **`CLAUSE` is not owned by one `DOCUMENT`** — it's owned by the
  `DOCUMENT_SERIES`, and linked to whichever document version(s) it appears
  in via `DOCUMENT_CLAUSE`. That's the dedup design: if a clause's
  `content_hash` is unchanged between Rev A and Rev B, no new `CLAUSE` row is
  created — the existing row just gets a second `DOCUMENT_CLAUSE` entry
  pointing at the Rev B document. This is what makes revision diffing
  (added/removed/unchanged clauses) and incremental re-check derivable
  directly from the schema, without a separate diff/lineage table.
- **`first_seen_document_id`** on `CLAUSE` is provenance only — which
  document version *introduced* this exact content — not the set of
  documents it currently appears in (that's `DOCUMENT_CLAUSE`'s job).
- **`JURISDICTION → JURISDICTION_DOCUMENT → JURISDICTION_CLAUSE`** is a
  separate, lighter-weight parallel chain for jurisdiction code reference
  material — no revision-tracking or cross-document dedup (yet), each
  jurisdiction document is ingested once, standalone. A `PROJECT` belongs to
  exactly one `JURISDICTION`.
- **`LLM_CALL`** is the audit record of one reasoning-engine invocation
  (prompt/raw response/tokens/latency), logged for every clause actually
  reasoned over — including ones that produced zero flags, since "why didn't
  this get flagged" is as much an audit question as "why did this get
  flagged." A `FLAG` always traces back to exactly one `LLM_CALL` via
  `llm_call_id`, so "what did the model see" is always answerable.
- **`check_type`** on both `LLM_CALL` and `FLAG` distinguishes the two check
  families (jurisdiction-compliance vs. cross-discipline coordination). Both
  families write rows scoped to `submission_id`, filtered by `check_type` so
  running one check can never touch the other's results.
- **Checks are incremental by default** (`app/check_persistence.py`): since
  `CLAUSE` rows are immutable and content-hash deduped, a clause only ever
  needs reasoning once per `check_type`, ever — a re-check skips any clause
  that already has an `LLM_CALL` of that type from *any* of the project's
  submissions, and only reasons about genuinely new/changed clauses. This is
  why "current flags for a project" is a computed view (every `FLAG` whose
  `clause_id` is still part of the project's current clause set), not a
  `submission_id` filter — a flag from an older submission stays "current"
  until the clause it's about is itself superseded. An explicit `force=True`
  clears a check_type's Flag/LLMCall rows project-wide and redoes everything.
- **`FLAG_CITATION`** has exactly one of `clause_id` / `jurisdiction_clause_id`
  set per row, matching `check_type`: jurisdiction checks cite
  `JURISDICTION_CLAUSE` rows, cross-discipline checks cite another project
  `CLAUSE` (both built — see `app/conflict_detection.py` and
  `app/cross_discipline_detection.py`).
- **`FLAG.status`** is a reviewer's disposition (open/acknowledged/resolved/
  false_positive), independent of `severity` (the model's own confidence
  label). Defaults to `open` on every flag; a reviewer sets it via the flags
  page, which also rolls it up into the reviewer-confirmed precision figure
  on `/metrics`. This is the "pilot alongside human review" hook — it lets a
  reviewer record what the tool got right or wrong without a separate system
  comparing against an independent review pass.
- **`FRESHNESS_SOURCE → FRESHNESS_SNAPSHOT/FRESHNESS_CHANGE`** is a separate
  chain feeding the jurisdiction-code-freshness POC
  (`app/freshness/*`), scheduled independently of any submission - every
  fetch writes a `FRESHNESS_SNAPSHOT` (for audit, even when nothing changed);
  a `FRESHNESS_CHANGE` row is written only when a fetch's content hash
  differs from the source's previous snapshot. `FRESHNESS_SOURCE.jurisdiction_id`
  is nullable because `icc_public` sources (base model code editions) apply
  across every jurisdiction, not one in particular - only `municode` sources
  are tied to a specific `JURISDICTION`.
- **A `municode`-kind `FRESHNESS_SOURCE`'s content also feeds
  `JURISDICTION_CLAUSE`** (`app/freshness/jurisdiction_sync.py`), not just the
  freshness dashboard: each scraped section becomes a `JURISDICTION_CLAUSE`
  row (`extraction_method = municode_scrape`) under a synthetic
  `JURISDICTION_DOCUMENT` (`doc_type = municode_scrape`), so it's picked up by
  the same retrieval pool (`find_candidate_jurisdiction_clauses`) as a
  human-uploaded document. That link is `JURISDICTION_DOCUMENT.metadata_
  ->> freshness_source_id`, not a real foreign key (no schema migration
  tooling exists yet to add one to an already-populated table), so it isn't
  drawn as a formal relationship above. Retrieval prefers whichever of an
  uploaded vs. scraped clause was more recently confirmed current on a
  near-tied match, via `JURISDICTION_DOCUMENT.ingested_at` - not "uploaded
  always wins," since a stale upload shouldn't outrank a same-day scrape.
