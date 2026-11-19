import { Router } from 'express';

import type { AuditFilters, AuditReader } from './audit.js';
import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';

function positiveInteger(value: string): number | undefined {
  const number = Number(value);
  return /^\d+$/.test(value) && Number.isSafeInteger(number) && number > 0 && number <= 2147483647
    ? number
    : undefined;
}

function utcDay(value: string): Date | undefined {
  const date = new Date(value + 'T00:00:00.000Z');
  return /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    !value.startsWith('0000') &&
    Number.isFinite(date.getTime()) &&
    date.toISOString().slice(0, 10) === value
    ? date
    : undefined;
}

export function parseAuditFilters(query: Record<string, unknown>): AuditFilters | undefined {
  const filters: AuditFilters = { limit: 25 };
  for (const [key, value] of Object.entries(query)) {
    if (typeof value !== 'string' || !value.trim() || value.length > 256) return undefined;
    switch (key) {
      case 'entity_type':
        filters.entityType = value;
        break;
      case 'entity_id':
        filters.entityId = value;
        break;
      case 'actor_user_id': {
        const actor = value === 'system' ? value : positiveInteger(value);
        if (actor === undefined) return undefined;
        filters.actorUserId = actor;
        break;
      }
      case 'from':
      case 'to': {
        const date = utcDay(value);
        if (!date) return undefined;
        filters[key] = key === 'to' ? new Date(date.getTime() + 86_400_000) : date;
        break;
      }
      case 'before_id':
      case 'limit': {
        const number = positiveInteger(value);
        if (number === undefined || (key === 'limit' && number > 100)) return undefined;
        if (key === 'limit') filters.limit = number;
        else filters.beforeId = number;
        break;
      }
      default:
        return undefined;
    }
  }
  if (filters.from && filters.to && filters.from >= filters.to) return undefined;
  return filters;
}

export function auditRouter(reader: AuditReader, guard: Guard): Router {
  const router = Router();
  router.get('/api/audit', guard('audit:read'), (request, response, next) => {
    const filters = parseAuditFilters(request.query);
    if (!filters) {
      response.status(422).json({ error: 'Invalid audit filters or pagination' });
      return;
    }
    response.set('Cache-Control', 'no-store');
    void reader
      .list(principal(response).orgId, filters)
      .then((result) => {
        response.json(result);
      })
      .catch(next);
  });
  return router;
}
