import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.addColumns('documents', {
    parent_document_id: { type: 'integer', references: 'documents', onDelete: 'CASCADE' },
  });
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropColumns('documents', ['parent_document_id']);
}
