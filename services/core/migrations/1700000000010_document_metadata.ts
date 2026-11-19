import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.addColumns('documents', {
    department: { type: 'text' },
    doc_date: { type: 'date' },
    mime_type: { type: 'text' },
    languages: { type: 'text[]', notNull: true, default: pgm.func("'{}'::text[]") },
    page_count: { type: 'integer' },
  });
  pgm.createIndex('documents', ['org_id', 'created_at', 'id'], {
    name: 'documents_org_created_idx',
  });
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropIndex('documents', ['org_id', 'created_at', 'id'], { name: 'documents_org_created_idx' });
  pgm.dropColumns('documents', ['department', 'doc_date', 'mime_type', 'languages', 'page_count']);
}
