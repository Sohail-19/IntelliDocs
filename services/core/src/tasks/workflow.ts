import type { TaskAction, TaskStatus } from '../../../../contracts/core-api.v1.js';
export type { TaskStatus } from '../../../../contracts/core-api.v1.js';

const transitions = {
  open: ['acknowledged', 'rejected', 'overdue'],
  acknowledged: ['in_progress', 'rejected'],
  in_progress: ['done', 'rejected'],
  overdue: ['acknowledged', 'in_progress', 'rejected'],
  done: [],
  rejected: [],
} as const satisfies Record<TaskStatus, readonly TaskStatus[]>;

export function allowedUserTransitions(from: TaskStatus): TaskAction[] {
  const allowed: readonly Exclude<TaskStatus, 'open'>[] = transitions[from];
  return allowed.filter((status): status is TaskAction => status !== 'overdue');
}

export interface TaskState {
  status: TaskStatus;
  rejection_reason: string | null;
}

export function isTaskStatus(value: unknown): value is TaskStatus {
  return typeof value === 'string' && Object.hasOwn(transitions, value);
}

export function taskTransition(
  from: TaskStatus,
  to: TaskStatus,
  reason: string | null,
  source: 'user' | 'system',
): TaskState | null {
  const allowed: readonly TaskStatus[] = transitions[from];
  if (!allowed.includes(to) || (to === 'overdue') !== (source === 'system')) return null;
  if (to === 'rejected' && !reason?.trim()) return null;
  return { status: to, rejection_reason: to === 'rejected' ? reason : null };
}
