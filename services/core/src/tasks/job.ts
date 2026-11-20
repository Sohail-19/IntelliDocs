import { randomUUID } from 'node:crypto';

import type { Pool } from 'pg';

import { inTransaction, writeAudit } from '../audit.js';
import { normalizedDeadline, resolvedDeadline } from '../obligation-values.js';
import { hasPendingStaleness, pendingStale } from '../relations/staleness.js';
import { changeAssignment } from '../routing/assignments.js';
import type { Assignment } from '../routing/assignments.js';
import { logEvent } from '../runtime.js';
import { lockTask, recordTaskTransition } from './repository.js';
import { taskTransition } from './workflow.js';

export class TaskJob {
  constructor(
    private readonly pool: Pool,
    private readonly reviewRoles: ReadonlyMap<string, string>,
  ) {}

  async run(now = new Date()): Promise<void> {
    const today = now.toISOString().slice(0, 10);
    const requestId = randomUUID();
    const reviewRoles = JSON.stringify(Object.fromEntries(this.reviewRoles));
    let afterId = 0;
    const batchSize = 100;
    while (true) {
      const { rows } = await this.pool.query<{ id: number; org_id: number }>(
        'SELECT o.id, d.org_id FROM obligations o JOIN documents d ON d.id = o.document_id ' +
          'JOIN organizations org ON org.id = d.org_id ' +
          "JOIN assignments a ON a.obligation_id = o.id AND a.status = 'active' " +
          'WHERE o.retired_at IS NULL AND o.external_id IS NOT NULL AND o.id > $4 AND NOT ' +
          pendingStale +
          ' ' +
          "AND ((o.task_status = 'open' AND " +
          resolvedDeadline +
          ' AND ' +
          normalizedDeadline +
          ' < $1) ' +
          "OR (o.task_status NOT IN ('done', 'rejected') AND a.escalation_due_at <= $2 " +
          'AND ($3::jsonb ->> org.slug) IS NOT NULL ' +
          "AND (a.queue_type IS DISTINCT FROM 'role' OR a.queue_name IS DISTINCT FROM ($3::jsonb ->> org.slug)))) " +
          'ORDER BY o.id LIMIT $5',
        [today, now, reviewRoles, afterId, batchSize],
      );
      for (const { id, org_id } of rows) {
        await this.process(id, org_id, today, now, requestId);
        afterId = id;
      }
      if (rows.length < batchSize) return;
    }
  }

  private async process(id: number, orgId: number, today: string, now: Date, requestId: string) {
    await inTransaction(this.pool, async (client) => {
      // Recheck task and assignment in the transaction after taking the obligation lock.
      const task = await lockTask(client, id, orgId, true);
      if (!task || (await hasPendingStaleness(client, id))) return;
      const { rows: assignments } = await client.query<Assignment>(
        "SELECT * FROM assignments WHERE obligation_id = $1 AND status = 'active'",
        [id],
      );
      const assignment = assignments[0];
      if (!assignment) return;
      const audit = { orgId, actorUserId: null, requestId, ip: null };
      const overdue = taskTransition(task.status, 'overdue', null, 'system');
      if (overdue && task.due_date !== null && task.due_date < today) {
        await recordTaskTransition(client, id, task, overdue, audit);
      }
      const role = this.reviewRoles.get(task.org_slug);
      if (
        !role ||
        task.status === 'done' ||
        task.status === 'rejected' ||
        assignment.escalation_due_at === null ||
        assignment.escalation_due_at > now ||
        (assignment.queue_type === 'role' && assignment.queue_name === role)
      ) {
        return;
      }
      const { before, after } = await changeAssignment(client, id, {
        assignee_user_id: null,
        queue_type: 'role',
        queue_name: role,
        rule_id: assignment.rule_id,
        reason: 'escalation',
        escalation_due_at: null,
      });
      const reference = { doc_id: task.doc_id, obligation_id: task.obligation_id };
      await writeAudit(client, audit, {
        action: 'task.escalate',
        entityType: 'task',
        entityId: String(id),
        before: { ...before, ...reference },
        after: { ...after, ...reference },
      });
    });
  }
}

export function taskIntervalMs(value = process.env.TASK_SWEEP_INTERVAL_SECONDS ?? '60'): number {
  const seconds = Number(value);
  if (!/^\d+$/.test(value) || seconds < 1 || seconds > 2147483) {
    throw new Error('TASK_SWEEP_INTERVAL_SECONDS must be an integer between 1 and 2147483');
  }
  return seconds * 1000;
}

export function scheduleTaskJob(job: TaskJob, intervalMs: number): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function tick() {
    try {
      await job.run();
    } catch {
      logEvent('task_sweep_failed');
    }
    if (!stopped) timer = setTimeout(() => void tick(), intervalMs).unref();
  }
  void tick();
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}
