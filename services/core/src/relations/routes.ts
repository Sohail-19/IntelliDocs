import { Router } from 'express';
import type { Request, RequestHandler, Response } from 'express';
import type { StaleDecision } from '../../../../contracts/core-api.v1.js';

import { auditContext } from '../audit.js';
import type { Guard } from '../auth/routes.js';
import { principal } from '../auth/routes.js';
import type { DocumentRelation } from '../intelligence/client.js';
import { RelationError } from './repository.js';
import type { PostgresRelationRepository, RelationReview } from './repository.js';

const relationTypes = [
  'supersedes',
  'amends',
  'withdraws',
  'conflicts_with',
  'references',
] as const;

function handle(run: (req: Request, res: Response) => Promise<void>): RequestHandler {
  return (req, res, next) => {
    res.set('Cache-Control', 'no-store');
    void run(req, res).catch((error: unknown) => {
      if (error instanceof RelationError) res.status(error.status).json({ error: error.message });
      else next(error);
    });
  };
}

function text(value: unknown, limit: number): value is string {
  return (
    typeof value === 'string' &&
    value.length <= limit &&
    Boolean(value.trim()) &&
    !value.includes('\0')
  );
}

function idOf(req: Request) {
  if (!text(req.params.id, 255)) throw new RelationError(422, 'Invalid relation ID');
  return req.params.id;
}

function bodyOf(req: Request): Record<string, unknown> {
  const body: unknown = req.body;
  if (!body || typeof body !== 'object' || Array.isArray(body))
    throw new RelationError(422, 'Expected a JSON object');
  return body as Record<string, unknown>;
}

function relationType(value: unknown): value is DocumentRelation['relation_type'] {
  return relationTypes.some((item) => item === value);
}

export function relationRouter(repository: PostgresRelationRepository, guard: Guard) {
  const router = Router();
  const review = guard('relations:review');
  router.get(
    '/api/relations',
    review,
    handle(async (req, res) => {
      const { status = 'proposed' } = req.query;
      if (status !== 'proposed' && status !== 'confirmed')
        throw new RelationError(422, 'Choose proposed or confirmed relations');
      res.json({ relations: await repository.list(principal(res).orgId, status) });
    }),
  );
  router.get(
    '/api/relations/:id',
    review,
    handle(async (req, res) => {
      res.json({ relation: await repository.get(principal(res).orgId, idOf(req)) });
    }),
  );
  function change(command: (req: Request) => RelationReview) {
    return handle(async (req, res) => {
      const relation = await repository.review(
        idOf(req),
        command(req),
        auditContext(req, res, principal(res)),
      );
      res.json({ relation });
    });
  }
  router.post(
    '/api/relations/:id/confirm',
    review,
    change(() => ({ action: 'confirm' })),
  );
  router.post(
    '/api/relations/:id/dismiss',
    review,
    change((req) => {
      const { reason } = bodyOf(req);
      if (!text(reason, 2000)) throw new RelationError(422, 'A dismissal reason is required');
      return { action: 'dismiss', reason: reason.trim() };
    }),
  );
  router.patch(
    '/api/relations/:id/type',
    review,
    change((req) => {
      const { relation_type } = bodyOf(req);
      if (!relationType(relation_type)) throw new RelationError(422, 'Invalid relation type');
      return { action: 'type', relationType: relation_type };
    }),
  );
  const stale = '/api/relations/:id/stale-obligations';
  router.get(
    stale,
    review,
    handle(async (req, res) => {
      res.json({ obligations: await repository.staleObligations(principal(res).orgId, idOf(req)) });
    }),
  );
  router.patch(
    stale + '/:obligation_id',
    review,
    handle(async (req, res) => {
      const id =
        typeof req.params.obligation_id === 'string' && /^\d{1,10}$/.test(req.params.obligation_id)
          ? Number(req.params.obligation_id)
          : 0;
      if (id < 1 || id > 2147483647) throw new RelationError(422, 'Invalid obligation ID');
      const { decision, reason, action } = bodyOf(req);
      if (!text(reason, 2000)) throw new RelationError(422, 'A stale review reason is required');
      let command: StaleDecision;
      if (decision === 'keep' || decision === 'close')
        command = { decision, reason: reason.trim() };
      else if (decision === 'update' && text(action, 2000))
        command = { decision, reason: reason.trim(), action: action.trim() };
      else throw new RelationError(422, 'Choose keep, close, or update with a replacement action');
      await repository.decideStaleness(
        idOf(req),
        id,
        command,
        auditContext(req, res, principal(res)),
      );
      res.sendStatus(204);
    }),
  );
  return router;
}
