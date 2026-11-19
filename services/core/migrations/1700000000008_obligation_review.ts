import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.addColumns('obligations', {
    owner_override: { type: 'jsonb' },
    deadline_override: { type: 'jsonb' },
    extracted_status: { type: 'text' },
    review_decision: { type: 'text', check: "review_decision IN ('approved', 'rejected')" },
    review_reason: { type: 'text' },
  });
  pgm.addConstraint('obligations', 'obligations_review_reason_check', {
    check:
      "(review_decision IS NOT DISTINCT FROM 'rejected' AND review_reason IS NOT NULL AND review_reason ~ '[^[:space:]]') OR " +
      "(review_decision IS DISTINCT FROM 'rejected' AND review_reason IS NULL)",
  });
  pgm.sql('UPDATE obligations SET extracted_status = status WHERE external_id IS NOT NULL');
  pgm.sql('DROP INDEX obligations_open_task_due_idx');
  pgm.sql(
    "CREATE INDEX obligations_open_task_due_idx ON obligations ((COALESCE(deadline_override, deadline)->>'normalized_date')) " +
      "WHERE retired_at IS NULL AND external_id IS NOT NULL AND task_status = 'open' " +
      "AND COALESCE(deadline_override, deadline)->'resolved' = 'true'::jsonb",
  );
}

export function down(pgm: MigrationBuilder): void {
  pgm.sql('DROP INDEX obligations_open_task_due_idx');
  pgm.sql(
    "CREATE INDEX obligations_open_task_due_idx ON obligations ((deadline->>'normalized_date')) " +
      "WHERE retired_at IS NULL AND external_id IS NOT NULL AND task_status = 'open' AND deadline->'resolved' = 'true'::jsonb",
  );
  pgm.dropConstraint('obligations', 'obligations_review_reason_check');
  pgm.dropColumns('obligations', [
    'owner_override',
    'deadline_override',
    'extracted_status',
    'review_decision',
    'review_reason',
  ]);
}
