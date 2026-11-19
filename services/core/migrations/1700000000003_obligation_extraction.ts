import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.addColumns('documents', {
    extraction_status: {
      type: 'text',
      notNull: true,
      default: 'pending',
      check: "extraction_status IN ('pending', 'completed', 'failed')",
    },
    extraction_error: { type: 'text' },
  });
  // Keep legacy rows readable in the existing table while adding the v1 extraction fields.
  pgm.addColumns('obligations', {
    external_id: { type: 'text' },
    owner_details: { type: 'jsonb' },
    deadline: { type: 'jsonb' },
    condition: { type: 'text' },
    obligation_type: { type: 'text' },
    citations: { type: 'jsonb', notNull: true, default: pgm.func("'[]'::jsonb") },
    confidence: { type: 'double precision', check: 'confidence BETWEEN 0 AND 1' },
    extractor_version: { type: 'text' },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('current_timestamp') },
  });
  pgm.addConstraint('obligations', 'obligations_document_external_id_key', {
    unique: ['document_id', 'external_id'],
  });
  pgm.addConstraint('obligations', 'obligations_extraction_fields_check', {
    check:
      "external_id IS NULL OR (owner_details IS NOT NULL AND deadline IS NOT NULL AND confidence IS NOT NULL AND extractor_version IS NOT NULL AND obligation_type IS NOT NULL AND status IN ('extracted', 'verified', 'needs_review', 'rejected'))",
  });
  pgm.createIndex('obligations', 'status', { name: 'obligations_extraction_status_idx' });
  pgm.sql(
    "CREATE INDEX obligations_owner_department_idx ON obligations ((owner_details->>'resolved_department'))",
  );
  pgm.sql("CREATE INDEX obligations_due_date_idx ON obligations ((deadline->>'normalized_date'))");
}

export function down(pgm: MigrationBuilder): void {
  pgm.sql('DROP INDEX obligations_due_date_idx');
  pgm.sql('DROP INDEX obligations_owner_department_idx');
  pgm.dropIndex('obligations', 'status', { name: 'obligations_extraction_status_idx' });
  pgm.dropConstraint('obligations', 'obligations_extraction_fields_check');
  pgm.dropConstraint('obligations', 'obligations_document_external_id_key');
  pgm.dropColumns('obligations', [
    'external_id',
    'owner_details',
    'deadline',
    'condition',
    'obligation_type',
    'citations',
    'confidence',
    'extractor_version',
    'updated_at',
  ]);
  pgm.dropColumns('documents', ['extraction_status', 'extraction_error']);
}
