# Document/Clause Storage — Entity Relationship Diagram

Reflects [app/models.py](../app/models.py). Source of truth is the code —
regenerate this by hand if the models change.

```mermaid
erDiagram
    PROJECT {
        string id PK
        string name
        string jurisdiction
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
        string extraction_method "pdf_text | ifc_property | manual"
        json location "page/bbox for 2D, ifc_guid/level/grid for BIM"
        string first_seen_document_id FK
        datetime created_at
    }

    DOCUMENT_CLAUSE {
        string document_id PK,FK
        string clause_id PK,FK
    }

    PROJECT ||--o{ SUBMISSION : "has revisions"
    PROJECT ||--o{ DOCUMENT_SERIES : "has sheets/specs"
    SUBMISSION ||--o{ DOCUMENT : "contains versions"
    DOCUMENT_SERIES ||--o{ DOCUMENT : "has versions"
    DOCUMENT_SERIES ||--o{ CLAUSE : "owns"
    DOCUMENT ||--o{ DOCUMENT_CLAUSE : "includes"
    CLAUSE ||--o{ DOCUMENT_CLAUSE : "appears in"
    DOCUMENT ||--o| CLAUSE : "first_seen_document_id"
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

## Not yet built

`flags` (conflict-detection output) and `flag_citations` will reference
`CLAUSE.id` directly once conflict detection is persisted — not shown here
since it doesn't exist in the code yet.
