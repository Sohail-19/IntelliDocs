import type { MigrationBuilder } from 'node-pg-migrate';

export const shorthands = undefined;

export function up(pgm: MigrationBuilder): void {
  pgm.createTable('users', {
    id: 'id',
    email: { type: 'varchar(320)', notNull: true, unique: true },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('current_timestamp') },
  });
  pgm.createTable('roles', {
    id: 'id',
    name: { type: 'varchar(100)', notNull: true, unique: true },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('current_timestamp') },
  });
  pgm.createTable('documents', {
    id: 'id',
    external_id: { type: 'varchar(255)', notNull: true, unique: true },
    filename: { type: 'text', notNull: true },
    status: { type: 'varchar(50)', notNull: true },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('current_timestamp') },
  });
  pgm.createTable('obligations', {
    id: 'id',
    document_id: { type: 'integer', notNull: true, references: 'documents', onDelete: 'CASCADE' },
    action: { type: 'text', notNull: true },
    owner: { type: 'text', notNull: true },
    status: { type: 'varchar(50)', notNull: true },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('current_timestamp') },
  });
  pgm.createTable('audit_log', {
    id: 'id',
    actor_id: { type: 'integer', references: 'users', onDelete: 'SET NULL' },
    event_type: { type: 'varchar(100)', notNull: true },
    payload: { type: 'jsonb', notNull: true, default: '{}' },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('current_timestamp') },
  });
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropTable('audit_log');
  pgm.dropTable('obligations');
  pgm.dropTable('documents');
  pgm.dropTable('roles');
  pgm.dropTable('users');
}
