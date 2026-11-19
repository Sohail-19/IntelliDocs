import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.addColumns('documents', { doc_type: { type: 'text' } });
  pgm.addColumns('obligations', { retired_at: { type: 'timestamptz' } });
  pgm.createTable('assignments', {
    id: 'id',
    obligation_id: { type: 'integer', notNull: true, references: 'obligations' },
    assignee_user_id: { type: 'integer', references: 'users' },
    queue_type: { type: 'text' },
    queue_name: { type: 'text' },
    rule_id: { type: 'text' },
    reason: { type: 'text', notNull: true },
    assigned_at: { type: 'timestamptz', notNull: true, default: pgm.func('current_timestamp') },
    escalation_due_at: { type: 'timestamptz' },
    status: { type: 'text', notNull: true, default: 'active' },
    closed_at: { type: 'timestamptz' },
  });
  pgm.addConstraint('assignments', 'assignments_target_check', {
    check:
      '(assignee_user_id IS NOT NULL AND queue_type IS NULL AND queue_name IS NULL) OR ' +
      "(assignee_user_id IS NULL AND queue_type IS NOT NULL AND queue_type IN ('role', 'department') AND queue_name IS NOT NULL)",
  });
  pgm.addConstraint('assignments', 'assignments_status_check', {
    check:
      "(status = 'active' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL)",
  });
  pgm.createIndex('assignments', 'obligation_id', {
    unique: true,
    where: "status = 'active'",
    name: 'assignments_one_active_idx',
  });
  pgm.createIndex('assignments', 'obligation_id', { name: 'assignments_history_idx' });
  pgm.sql(
    'INSERT INTO assignments (obligation_id, assignee_user_id, reason, assigned_at) ' +
      "SELECT id, assigned_user_id, 'manual', updated_at FROM obligations WHERE assigned_user_id IS NOT NULL",
  );
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropTable('assignments');
  pgm.dropColumns('obligations', ['retired_at']);
  pgm.dropColumns('documents', ['doc_type']);
}
