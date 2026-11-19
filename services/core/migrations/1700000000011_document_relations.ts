import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.sql(`
    ALTER TABLE documents ADD COLUMN authority_status text NOT NULL DEFAULT 'in_force'
      CHECK (authority_status IN ('in_force','superseded','partially_amended','withdrawn','under_review')),
      ADD CONSTRAINT documents_org_id_id_key UNIQUE (org_id, id);

    CREATE TABLE document_relations (
      org_id integer NOT NULL REFERENCES organizations,
      id text NOT NULL,
      source_id integer NOT NULL,
      target_id integer NOT NULL,
      relation_type text NOT NULL CHECK (relation_type IN ('supersedes','amends','withdraws','conflicts_with','references')),
      scope text NOT NULL CHECK (scope IN ('whole_document','clauses')),
      source_citations jsonb NOT NULL CHECK (jsonb_typeof(source_citations) = 'array'),
      target_citations jsonb NOT NULL CHECK (jsonb_typeof(target_citations) = 'array'),
      confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
      confidence_breakdown jsonb,
      status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','confirmed','dismissed')),
      needs_review boolean,
      detected_by text NOT NULL,
      created_at timestamptz NOT NULL,
      reviewed_by integer,
      reviewed_at timestamptz,
      review_reason text,
      PRIMARY KEY (org_id, id),
      FOREIGN KEY (org_id, source_id) REFERENCES documents (org_id, id),
      FOREIGN KEY (org_id, target_id) REFERENCES documents (org_id, id),
      FOREIGN KEY (org_id, reviewed_by) REFERENCES users (org_id, id),
      CHECK (source_id <> target_id),
      CHECK (status <> 'confirmed' OR
        (needs_review IS NOT TRUE AND jsonb_array_length(source_citations) > 0
         AND jsonb_array_length(target_citations) > 0))
    );
    CREATE INDEX document_relations_target_idx ON document_relations (org_id, target_id, status);
    CREATE INDEX document_relations_review_idx ON document_relations (org_id, status, created_at DESC, id);

    CREATE TABLE obligation_staleness (
      org_id integer NOT NULL,
      obligation_id integer NOT NULL REFERENCES obligations,
      relation_id text NOT NULL,
      decision text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','keep','update','close')),
      flagged_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
      decided_by integer,
      decided_at timestamptz,
      reason text,
      PRIMARY KEY (obligation_id, relation_id),
      FOREIGN KEY (org_id, relation_id) REFERENCES document_relations (org_id, id),
      FOREIGN KEY (org_id, decided_by) REFERENCES users (org_id, id)
    );
    CREATE INDEX obligation_pending_staleness_idx ON obligation_staleness (obligation_id)
      WHERE decision = 'pending';
    ALTER TABLE obligations ADD COLUMN stale_action_override text,
      ADD COLUMN stale_citations_override jsonb;
    CREATE INDEX obligation_effective_citations_idx
      ON obligations USING gin ((COALESCE(stale_citations_override, citations)) jsonb_path_ops);
  `);
}

export function down(pgm: MigrationBuilder): void {
  pgm.sql(`
    DROP INDEX obligation_effective_citations_idx;
    ALTER TABLE obligations DROP COLUMN stale_action_override, DROP COLUMN stale_citations_override;
    DROP TABLE obligation_staleness;
    DROP TABLE document_relations;
    ALTER TABLE documents DROP CONSTRAINT documents_org_id_id_key, DROP COLUMN authority_status;
  `);
}
