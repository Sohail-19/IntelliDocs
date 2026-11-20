import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.sql(`
    ALTER TABLE documents ADD COLUMN demo_upload boolean NOT NULL DEFAULT false;
    CREATE INDEX documents_demo_expiry ON documents (created_at, id)
      WHERE demo_upload AND parent_document_id IS NULL;
    CREATE TABLE demo_upload_usage (
      day date NOT NULL,
      actor_id integer NOT NULL,
      used integer NOT NULL CHECK (used > 0),
      PRIMARY KEY (day, actor_id)
    );
  `);
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropTable('demo_upload_usage');
  pgm.sql('DROP INDEX documents_demo_expiry; ALTER TABLE documents DROP COLUMN demo_upload;');
}
