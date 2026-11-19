import type { Pool, PoolClient } from 'pg';
import type { TaskFeed, TaskRecord, TaskScope } from '../../../../contracts/core-api.v1.js';

import { inTransaction, writeAudit } from '../audit.js';
import type { AuditContext } from '../audit.js';
import type { Principal } from '../auth/policy.js';
import { normalizedDeadline, resolvedDeadline } from '../obligation-values.js';
import {
  effectiveAction,
  effectiveCitations,
  pendingStale,
  pendingStaleIds,
} from '../relations/staleness.js';
import { allowedUserTransitions, taskTransition } from './workflow.js';
import type { TaskState, TaskStatus } from './workflow.js';

export type { TaskScope } from '../../../../contracts/core-api.v1.js';

export interface TaskFilters {
  scope?: TaskScope;
  status?: TaskStatus;
  doc_id?: string;
  due_from?: string;
  due_to?: string;
}

type TaskRow = Omit<TaskRecord, 'allowed_transitions'> & { obligation_status: string };

function toTask({ obligation_status, ...row }: TaskRow, canTransition: boolean): TaskRecord {
  return {
    ...row,
    allowed_transitions:
      canTransition &&
      !row.stale &&
      obligation_status !== 'needs_review' &&
      obligation_status !== 'rejected'
        ? allowedUserTransitions(row.status)
        : [],
  };
}

export const taskDueDate = 'CASE WHEN ' + resolvedDeadline + ' THEN ' + normalizedDeadline + ' END';

export async function lockTask(client: PoolClient, id: number, orgId: number, skipLocked = false) {
  const { rows } = await client.query<
    TaskState &
      Pick<TaskRecord, 'doc_id' | 'due_date' | 'obligation_id'> & {
        org_slug: string;
        obligation_status: string;
      }
  >(
    'SELECT o.task_status AS status, o.task_rejection_reason AS rejection_reason, ' +
      'o.external_id AS obligation_id, o.status AS obligation_status, d.external_id AS doc_id, org.slug AS org_slug, ' +
      taskDueDate +
      ' AS due_date FROM obligations o JOIN documents d ON d.id = o.document_id ' +
      'JOIN organizations org ON org.id = d.org_id ' +
      'WHERE o.id = $1 AND d.org_id = $2 AND o.retired_at IS NULL AND o.external_id IS NOT NULL ' +
      'FOR UPDATE OF o' +
      (skipLocked ? ' SKIP LOCKED' : ''),
    [id, orgId],
  );
  return rows[0];
}

export async function recordTaskTransition(
  client: PoolClient,
  id: number,
  task: TaskState & Pick<TaskRecord, 'doc_id' | 'obligation_id'>,
  next: TaskState,
  audit: AuditContext,
) {
  await client.query(
    'UPDATE obligations SET task_status = $2, task_rejection_reason = $3, updated_at = CURRENT_TIMESTAMP WHERE id = $1',
    [id, next.status, next.rejection_reason],
  );
  const { doc_id, obligation_id, status, rejection_reason } = task;
  await writeAudit(client, audit, {
    action: 'task.transition',
    entityType: 'task',
    entityId: String(id),
    before: { status, rejection_reason, doc_id, obligation_id },
    after: { ...next, doc_id, obligation_id },
  });
}

export class TaskRepository {
  constructor(
    private readonly pool: Pool,
    private readonly reviewRoles: ReadonlyMap<string, string>,
  ) {}

  async feed(
    actor: Principal,
    filters: TaskFilters,
    canTransition: boolean,
    reviewStale = false,
  ): Promise<TaskFeed> {
    const [rows, membership] = await Promise.all([
      this.visibleTasks(this.pool, actor, filters),
      this.pool.query<{ department_id: number | null }>(
        'SELECT department_id FROM users WHERE id = $1 AND org_id = $2 AND active',
        [actor.id, actor.orgId],
      ),
    ]);
    const available_scopes: TaskScope[] = ['mine'];
    if (membership.rows[0]?.department_id != null) available_scopes.push('department');
    const reviewRole = this.reviewRoles.get(actor.orgSlug);
    if (reviewRole && actor.roles.includes(reviewRole)) available_scopes.push('review');
    if (reviewStale) available_scopes.push('stale');
    return { tasks: rows.map((row) => toTask(row, canTransition)), available_scopes };
  }

  async hasVisibleTask(actor: Principal, docId: string): Promise<boolean> {
    return (await this.visibleTasks(this.pool, actor, {}, undefined, docId)).length > 0;
  }

  async transition(
    actor: Principal,
    id: number,
    status: TaskStatus,
    reason: string | null,
    audit: AuditContext,
  ): Promise<TaskRecord | 'missing' | 'invalid'> {
    return inTransaction(this.pool, async (client) => {
      const locked = await lockTask(client, id, actor.orgId);
      if (!locked) return 'missing';
      // Assignment changes take the same obligation lock. Check visibility after acquiring it.
      const [task] = await this.visibleTasks(client, actor, {}, id);
      if (!task) return 'missing';
      if (task.stale) return 'invalid';
      if (locked.obligation_status === 'needs_review' || locked.obligation_status === 'rejected')
        return 'invalid';
      const next = taskTransition(task.status, status, reason, 'user');
      if (!next) return 'invalid';
      await recordTaskTransition(client, id, task, next, audit);
      return toTask(
        {
          ...task,
          ...next,
          overdue: next.status === 'done' || next.status === 'rejected' ? false : task.overdue,
        },
        true,
      );
    });
  }

  private async visibleTasks(
    client: Pool | PoolClient,
    actor: Principal,
    filters: TaskFilters,
    id?: number,
    sourceDocId?: string,
  ) {
    const clauses = ['scope IS NOT NULL'];
    const params: (string | number | string[] | null)[] = [
      actor.id,
      actor.orgId,
      actor.roles,
      this.reviewRoles.get(actor.orgSlug) ?? null,
    ];
    for (const [value, predicate] of [
      [id, 'id ='],
      [filters.scope, 'scope ='],
      [filters.status, 'status ='],
      [filters.doc_id, 'doc_id ='],
      [filters.due_from, 'due_date >='],
      [filters.due_to, 'due_date <='],
    ] as const) {
      if (value === undefined) continue;
      params.push(value);
      clauses.push(predicate + ' $' + params.length);
    }
    if (sourceDocId !== undefined) {
      params.push(sourceDocId, JSON.stringify([{ doc_id: sourceDocId }]));
      clauses.push(
        '(doc_id = $' + (params.length - 1) + ' OR citations @> $' + params.length + '::jsonb)',
      );
    }
    const { rows } = await client.query<TaskRow>(
      "SELECT *, status = 'overdue' OR (status NOT IN ('done', 'rejected') AND COALESCE(due_date < (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date::text, false)) AS overdue FROM (" +
        'SELECT o.id, o.external_id AS obligation_id, d.external_id AS doc_id, ' +
        effectiveAction +
        ' AS action, ' +
        effectiveCitations +
        ' AS citations, ' +
        pendingStale +
        ' AS stale, ' +
        pendingStaleIds +
        ' AS stale_relation_ids, ' +
        'o.task_status AS status, o.status AS obligation_status, o.task_rejection_reason AS rejection_reason, ' +
        taskDueDate +
        ' AS due_date, a.assignee_user_id, a.queue_type, a.queue_name, COALESCE(assignee.name, a.queue_name) AS owner, ' +
        'CASE ' +
        (filters.scope === 'stale' ? 'WHEN ' + pendingStale + " THEN 'stale' " : '') +
        "WHEN a.assignee_user_id = u.id THEN 'mine' " +
        "WHEN a.queue_type = 'department' AND a.queue_name = dep.name THEN 'department' " +
        "WHEN a.queue_type = 'role' AND a.queue_name = ANY($3::text[]) THEN " +
        "CASE WHEN a.queue_name = $4 THEN 'review' ELSE 'mine' END END AS scope " +
        'FROM obligations o JOIN documents d ON d.id = o.document_id ' +
        "JOIN assignments a ON a.obligation_id = o.id AND a.status = 'active' " +
        'JOIN users u ON u.id = $1 AND u.org_id = d.org_id AND u.active ' +
        'LEFT JOIN users assignee ON assignee.id = a.assignee_user_id AND assignee.org_id = d.org_id ' +
        'LEFT JOIN departments dep ON dep.id = u.department_id AND dep.org_id = u.org_id ' +
        'WHERE d.org_id = $2 AND o.retired_at IS NULL AND o.external_id IS NOT NULL' +
        ') AS feed WHERE ' +
        clauses.join(' AND ') +
        ' ORDER BY due_date ASC NULLS LAST, id',
      params,
    );
    return rows;
  }
}
