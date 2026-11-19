import { randomUUID } from 'node:crypto';
import { isIP } from 'node:net';

import type { Request, RequestHandler, Response } from 'express';
import type { Pool, PoolClient } from 'pg';
import type { AuditEntry as AuditEntryWire } from '../../../contracts/core-api.v1.js';

export interface AuditRequest {
  requestId: string;
  ip: string | null;
}

export interface AuditContext extends AuditRequest {
  orgId: number;
  actorUserId: number | null;
}

export interface AuditEvent {
  action: string;
  entityType: string;
  entityId: string;
  before: object | null;
  after: object | null;
}

export interface AuditFilters {
  action?: string;
  entityType?: string;
  entityId?: string;
  actorUserId?: number | 'system';
  from?: Date;
  to?: Date;
  beforeId?: number;
  limit: number;
}

export type AuditEntry = Omit<AuditEntryWire, 'timestamp'> & { timestamp: Date };

export interface AuditReader {
  list(
    orgId: number,
    filters: AuditFilters,
  ): Promise<{
    entries: AuditEntry[];
    next_cursor: number | null;
  }>;
}

export const assignRequestId: RequestHandler = (_request, response, next) => {
  const id = randomUUID();
  response.locals.requestId = id;
  response.set('X-Request-ID', id);
  next();
};

export function auditRequest(request: Request, response: Response): AuditRequest {
  const requestId: unknown = response.locals.requestId;
  if (typeof requestId !== 'string') throw new Error('Request ID middleware is required');
  const ip = request.ip;
  return { requestId, ip: ip && isIP(ip) ? ip : null };
}

export function auditContext(
  request: Request,
  response: Response,
  actor: { orgId: number; id: number },
): AuditContext {
  return { ...auditRequest(request, response), orgId: actor.orgId, actorUserId: actor.id };
}

export async function inTransaction<T>(
  pool: Pool,
  operation: (client: PoolClient) => Promise<T>,
): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const result = await operation(client);
    await client.query('COMMIT');
    return result;
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

/** Call using the same transaction client as the associated mutation. */
export async function writeAudit(
  client: PoolClient,
  context: AuditContext,
  event: AuditEvent,
): Promise<void> {
  await client.query(
    'INSERT INTO audit_log (org_id, actor_user_id, action, entity_type, entity_id, "before", "after", request_id, ip) ' +
      'VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9::inet)',
    [
      context.orgId,
      context.actorUserId,
      event.action,
      event.entityType,
      event.entityId,
      event.before === null ? null : JSON.stringify(event.before),
      event.after === null ? null : JSON.stringify(event.after),
      context.requestId,
      context.ip,
    ],
  );
}

export class PostgresAuditReader implements AuditReader {
  constructor(private readonly pool: Pool) {}

  async list(orgId: number, filters: AuditFilters) {
    const clauses = ['org_id = $1'];
    const values: (number | string | Date)[] = [orgId];
    if (filters.actorUserId === 'system') clauses.push('actor_user_id IS NULL');
    for (const [value, predicate] of [
      [filters.action, 'action ='],
      [filters.entityType, 'entity_type ='],
      [filters.entityId, 'entity_id ='],
      [filters.actorUserId === 'system' ? undefined : filters.actorUserId, 'actor_user_id ='],
      [filters.from, '"timestamp" >='],
      [filters.to, '"timestamp" <'],
      [filters.beforeId, 'id <'],
    ] as const) {
      if (value === undefined) continue;
      values.push(value);
      clauses.push(predicate + ' $' + values.length);
    }
    values.push(filters.limit + 1);
    const { rows } = await this.pool.query<AuditEntry>(
      'SELECT id, "timestamp", actor_user_id, action, entity_type, entity_id, "before", "after", ' +
        'request_id, host(ip) AS ip FROM audit_log WHERE ' +
        clauses.join(' AND ') +
        ' ORDER BY id DESC LIMIT $' +
        values.length,
      values,
    );
    const entries = rows.slice(0, filters.limit);
    return {
      entries,
      next_cursor: rows.length > filters.limit ? (entries.at(-1)?.id ?? null) : null,
    };
  }
}
