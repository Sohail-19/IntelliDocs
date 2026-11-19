import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.addColumns('obligations', {
    task_status: {
      type: 'text',
      notNull: true,
      default: 'open',
      check:
        "task_status IN ('open', 'acknowledged', 'in_progress', 'done', 'rejected', 'overdue')",
    },
    task_rejection_reason: { type: 'text' },
  });
  pgm.addConstraint('obligations', 'obligations_task_rejection_check', {
    check:
      "(task_status = 'rejected' AND task_rejection_reason IS NOT NULL AND task_rejection_reason ~ '[^[:space:]]') OR " +
      "(task_status <> 'rejected' AND task_rejection_reason IS NULL)",
  });
  pgm.sql(
    "CREATE INDEX obligations_open_task_due_idx ON obligations ((deadline->>'normalized_date')) " +
      "WHERE retired_at IS NULL AND external_id IS NOT NULL AND task_status = 'open' AND deadline->'resolved' = 'true'::jsonb",
  );
  pgm.createIndex('assignments', 'escalation_due_at', {
    name: 'assignments_pending_escalation_idx',
    where: "status = 'active' AND escalation_due_at IS NOT NULL",
  });
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropIndex('assignments', 'escalation_due_at', { name: 'assignments_pending_escalation_idx' });
  pgm.sql('DROP INDEX obligations_open_task_due_idx');
  pgm.dropConstraint('obligations', 'obligations_task_rejection_check');
  pgm.dropColumns('obligations', ['task_status', 'task_rejection_reason']);
}
