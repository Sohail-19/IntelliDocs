import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.sql(
    "UPDATE documents SET status = CASE WHEN status = 'failed' THEN 'failed' WHEN status = 'indexed' THEN 'indexed' ELSE 'parsed' END",
  );
  pgm.addConstraint('documents', 'documents_index_status_check', {
    check: "status IN ('parsed', 'indexed', 'failed')",
  });
  pgm.addColumns('documents', {
    content_hash: { type: 'varchar(64)' },
    index_error: { type: 'text' },
    indexed_at: { type: 'timestamptz' },
  });
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropColumns('documents', ['content_hash', 'index_error', 'indexed_at']);
  pgm.dropConstraint('documents', 'documents_index_status_check');
  pgm.sql("UPDATE documents SET status = 'ready' WHERE status IN ('parsed', 'indexed')");
}
