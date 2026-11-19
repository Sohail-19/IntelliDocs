import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.createTable('organizations', {
    id: 'id',
    slug: { type: 'text', notNull: true, unique: true },
  });
  pgm.createTable('departments', {
    id: 'id',
    org_id: { type: 'integer', notNull: true, references: 'organizations' },
    name: { type: 'text', notNull: true },
  });
  pgm.addConstraint('departments', 'departments_org_name_key', { unique: ['org_id', 'name'] });
  pgm.addConstraint('departments', 'departments_org_id_key', { unique: ['org_id', 'id'] });

  pgm.addColumns('users', {
    org_id: { type: 'integer', references: 'organizations' },
    department_id: { type: 'integer' },
    password_hash: { type: 'text' },
    name: { type: 'text' },
    active: { type: 'boolean', notNull: true, default: false },
  });
  pgm.dropConstraint('users', 'users_email_key');
  pgm.addConstraint('users', 'users_org_email_key', { unique: ['org_id', 'email'] });
  pgm.addConstraint('users', 'users_org_id_key', { unique: ['org_id', 'id'] });
  pgm.addConstraint('users', 'users_department_fk', {
    foreignKeys: { columns: ['org_id', 'department_id'], references: 'departments(org_id, id)' },
  });
  pgm.addConstraint('users', 'users_active_credentials_check', {
    check: 'NOT active OR (org_id IS NOT NULL AND password_hash IS NOT NULL AND name IS NOT NULL)',
  });

  pgm.addColumns('roles', { org_id: { type: 'integer', references: 'organizations' } });
  pgm.dropConstraint('roles', 'roles_name_key');
  pgm.addConstraint('roles', 'roles_org_name_key', { unique: ['org_id', 'name'] });
  pgm.addConstraint('roles', 'roles_org_id_key', { unique: ['org_id', 'id'] });
  pgm.createTable('user_roles', {
    org_id: { type: 'integer', notNull: true },
    user_id: { type: 'integer', notNull: true },
    role_id: { type: 'integer', notNull: true },
  });
  pgm.addConstraint('user_roles', 'user_roles_pkey', { primaryKey: ['user_id', 'role_id'] });
  pgm.addConstraint('user_roles', 'user_roles_user_fk', {
    foreignKeys: {
      columns: ['org_id', 'user_id'],
      references: 'users(org_id, id)',
      onDelete: 'CASCADE',
    },
  });
  pgm.addConstraint('user_roles', 'user_roles_role_fk', {
    foreignKeys: {
      columns: ['org_id', 'role_id'],
      references: 'roles(org_id, id)',
      onDelete: 'CASCADE',
    },
  });
  pgm.createTable('refresh_tokens', {
    token_hash: { type: 'text', primaryKey: true },
    user_id: { type: 'integer', notNull: true, references: 'users', onDelete: 'CASCADE' },
    expires_at: { type: 'timestamptz', notNull: true },
  });
  pgm.createIndex('refresh_tokens', 'expires_at');
  pgm.addColumns('documents', { org_id: { type: 'integer', references: 'organizations' } });
  pgm.createIndex('documents', 'org_id');
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropColumns('documents', ['org_id']);
  pgm.dropTable('refresh_tokens');
  pgm.dropTable('user_roles');
  pgm.dropConstraint('roles', 'roles_org_name_key');
  pgm.dropConstraint('roles', 'roles_org_id_key');
  pgm.dropColumns('roles', ['org_id']);
  pgm.addConstraint('roles', 'roles_name_key', { unique: 'name' });
  pgm.dropConstraint('users', 'users_department_fk');
  pgm.dropConstraint('users', 'users_org_email_key');
  pgm.dropConstraint('users', 'users_org_id_key');
  pgm.dropConstraint('users', 'users_active_credentials_check');
  pgm.dropColumns('users', ['org_id', 'department_id', 'password_hash', 'name', 'active']);
  pgm.addConstraint('users', 'users_email_key', { unique: 'email' });
  pgm.dropTable('departments');
  pgm.dropTable('organizations');
}
