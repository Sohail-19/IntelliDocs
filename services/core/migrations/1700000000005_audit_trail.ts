import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.renameColumn('audit_log', 'created_at', 'timestamp');
  pgm.renameColumn('audit_log', 'actor_id', 'actor_user_id');
  pgm.renameColumn('audit_log', 'event_type', 'action');
  pgm.renameColumn('audit_log', 'payload', 'after');
  pgm.dropConstraint('audit_log', 'audit_log_actor_id_fkey');
  pgm.addConstraint('audit_log', 'audit_actor_user_fk', {
    foreignKeys: { columns: 'actor_user_id', references: 'users' },
  });
  pgm.alterColumn('audit_log', 'after', { notNull: false, default: null });
  pgm.addColumns('audit_log', {
    org_id: { type: 'integer', references: 'organizations' },
    entity_type: { type: 'text' },
    entity_id: { type: 'text' },
    before: { type: 'jsonb' },
    request_id: { type: 'text' },
    ip: { type: 'inet' },
  });
  pgm.sql(`
    UPDATE audit_log SET entity_type = CASE WHEN "after" ? 'doc_id' THEN 'document' ELSE 'legacy' END,
      entity_id = COALESCE("after"->>'doc_id', id::text), request_id = 'legacy-' || id;
    UPDATE audit_log a SET org_id = d.org_id FROM documents d
      WHERE a.entity_type = 'document' AND a.entity_id = d.external_id;
    UPDATE audit_log a SET org_id = u.org_id FROM users u
      WHERE a.org_id IS NULL AND a.actor_user_id = u.id;
  `);
  pgm.alterColumn('audit_log', 'entity_type', { notNull: true });
  pgm.alterColumn('audit_log', 'entity_id', { notNull: true });
  pgm.alterColumn('audit_log', 'request_id', { notNull: true });
  pgm.createIndex('audit_log', ['org_id', 'id'], { name: 'audit_org_cursor_idx' });
  pgm.createIndex('audit_log', ['org_id', 'entity_type', 'entity_id'], {
    name: 'audit_entity_idx',
  });
  pgm.createIndex('audit_log', ['org_id', 'actor_user_id'], { name: 'audit_actor_idx' });
  pgm.createIndex('audit_log', ['org_id', 'timestamp'], { name: 'audit_timestamp_idx' });
  pgm.sql(`
    CREATE FUNCTION prevent_audit_log_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = '55000';
    END;
    $$;
    CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
      FOR EACH STATEMENT EXECUTE FUNCTION prevent_audit_log_mutation();
  `);
  pgm.addColumns('obligations', {
    assigned_user_id: { type: 'integer', references: 'users' },
  });
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropColumns('obligations', ['assigned_user_id']);
  pgm.sql(
    'DROP TRIGGER audit_log_append_only ON audit_log; DROP FUNCTION prevent_audit_log_mutation();',
  );
  pgm.dropColumns('audit_log', [
    'org_id',
    'entity_type',
    'entity_id',
    'before',
    'request_id',
    'ip',
  ]);
  pgm.sql(`UPDATE audit_log SET "after" = '{}'::jsonb WHERE "after" IS NULL`);
  pgm.alterColumn('audit_log', 'after', { notNull: true, default: '{}' });
  pgm.renameColumn('audit_log', 'timestamp', 'created_at');
  pgm.dropConstraint('audit_log', 'audit_actor_user_fk');
  pgm.renameColumn('audit_log', 'actor_user_id', 'actor_id');
  pgm.addConstraint('audit_log', 'audit_log_actor_id_fkey', {
    foreignKeys: { columns: 'actor_id', references: 'users', onDelete: 'SET NULL' },
  });
  pgm.renameColumn('audit_log', 'action', 'event_type');
  pgm.renameColumn('audit_log', 'after', 'payload');
}
