import type { PoolClient } from 'pg';

export interface AssignmentTarget {
  assignee_user_id: number | null;
  queue_type: 'role' | 'department' | null;
  queue_name: string | null;
  rule_id: string | null;
  escalation_due_at: Date | null;
  reason: 'manual' | 'rule' | 'review' | 'unavailable_user' | 'escalation';
}

export interface Assignment extends AssignmentTarget {
  id: number;
  assigned_at: Date;
  status: 'active' | 'closed';
  closed_at: Date | null;
}

/** The caller holds the obligation row lock, or has just inserted that row. */
export async function changeAssignment(
  client: PoolClient,
  obligationId: number,
  target: AssignmentTarget | null,
  keepExisting = false,
): Promise<{ before: Assignment | null; after: Assignment | null; changed: boolean }> {
  const previous = await client.query<Assignment>(
    "SELECT * FROM assignments WHERE obligation_id = $1 AND status = 'active'",
    [obligationId],
  );
  const before = previous.rows[0] ?? null;
  if (
    keepExisting &&
    before &&
    target &&
    ((before.reason === 'manual' && target.reason !== 'review') ||
      (before.reason === 'escalation' &&
        target.reason === 'rule' &&
        before.rule_id === target.rule_id) ||
      (before.assignee_user_id === target.assignee_user_id &&
        before.queue_type === target.queue_type &&
        before.queue_name === target.queue_name &&
        before.rule_id === target.rule_id &&
        before.reason === target.reason))
  ) {
    return { before, after: before, changed: false };
  }
  if (before) {
    await client.query(
      "UPDATE assignments SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = $1",
      [before.id],
    );
  }
  let after = null;
  if (target) {
    const created = await client.query<Assignment>(
      'INSERT INTO assignments (obligation_id, assignee_user_id, queue_type, queue_name, rule_id, escalation_due_at, reason) ' +
        'VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *',
      [
        obligationId,
        target.assignee_user_id,
        target.queue_type,
        target.queue_name,
        target.rule_id,
        target.escalation_due_at,
        target.reason,
      ],
    );
    after = created.rows[0] ?? null;
    if (!after) throw new Error('Assignment insert returned no row');
  }
  await client.query(
    'UPDATE obligations SET assigned_user_id = $2, updated_at = CURRENT_TIMESTAMP WHERE id = $1',
    [obligationId, target?.assignee_user_id ?? null],
  );
  return { before, after, changed: true };
}
