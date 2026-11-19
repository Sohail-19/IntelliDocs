import { Router } from 'express';

import { auditContext } from '../audit.js';
import { principal } from '../auth/routes.js';
import type { Guard } from '../auth/routes.js';
import { isCalendarDate } from '../obligation-values.js';
import type { TaskFilters, TaskRepository } from './repository.js';
import { isTaskStatus } from './workflow.js';

function parseFilters(query: Record<string, unknown>): TaskFilters | undefined {
  const filters: TaskFilters = {};
  for (const [key, value] of Object.entries(query)) {
    if (typeof value !== 'string' || !value.trim() || value.length > 256 || value.includes('\0')) {
      return undefined;
    }
    switch (key) {
      case 'scope':
        if (value !== 'mine' && value !== 'department' && value !== 'review') return undefined;
        filters.scope = value;
        break;
      case 'status':
        if (!isTaskStatus(value)) return undefined;
        filters.status = value;
        break;
      case 'doc_id':
        filters.doc_id = value;
        break;
      case 'due_from':
      case 'due_to':
        if (!isCalendarDate(value)) return undefined;
        filters[key] = value;
        break;
      default:
        return undefined;
    }
  }
  if (filters.due_from && filters.due_to && filters.due_from > filters.due_to) return undefined;
  return filters;
}

export function taskRouter(repository: TaskRepository, guard: Guard): Router {
  const router = Router();
  router.get('/api/tasks', guard('tasks:read'), (request, response, next) => {
    const filters = parseFilters(request.query);
    if (!filters) {
      response.status(422).json({ error: 'Invalid task filters' });
      return;
    }
    response.set('Cache-Control', 'no-store');
    void repository
      .list(principal(response), filters)
      .then((tasks) => {
        response.json({ tasks });
      })
      .catch(next);
  });

  router.patch('/api/tasks/:id/status', guard('tasks:transition'), (request, response, next) => {
    const param = request.params.id;
    const id = typeof param === 'string' && /^\d{1,10}$/.test(param) ? Number(param) : 0;
    if (id < 1 || id > 2147483647) {
      response.status(422).json({ error: 'Invalid task ID' });
      return;
    }
    const body: unknown = request.body;
    const { status, reason } =
      typeof body === 'object' && body !== null && !Array.isArray(body)
        ? (body as Record<string, unknown>)
        : {};
    if (!isTaskStatus(status)) {
      response.status(422).json({ error: 'Invalid task status' });
      return;
    }
    if (
      status === 'rejected' &&
      (typeof reason !== 'string' ||
        !reason.trim() ||
        reason.length > 2000 ||
        reason.includes('\0'))
    ) {
      response.status(422).json({ error: 'Rejection requires a reason of up to 2000 characters' });
      return;
    }
    const actor = principal(response);
    response.set('Cache-Control', 'no-store');
    void repository
      .transition(
        actor,
        id,
        status,
        typeof reason === 'string' && status === 'rejected' ? reason.trim() : null,
        auditContext(request, response, actor),
      )
      .then((result) => {
        if (result === 'missing') response.status(404).json({ error: 'Task not found' });
        else if (result === 'invalid') {
          response.status(409).json({ error: 'Task transition is not allowed' });
        } else response.json({ task: result });
      })
      .catch(next);
  });
  return router;
}
