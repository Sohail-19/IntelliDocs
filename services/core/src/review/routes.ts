import { Router } from 'express';
import type { Request, RequestHandler, Response } from 'express';

import { auditContext } from '../audit.js';
import { principal } from '../auth/routes.js';
import type { Guard } from '../auth/routes.js';
import { isCalendarDate } from '../obligation-values.js';
import { ReviewError } from './repository.js';
import type { ObligationReviewRepository, ReviewChange } from './repository.js';

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ReviewError(422, 'Expected a JSON object');
  }
  return value as Record<string, unknown>;
}

function nonblank(value: unknown, limit: number): value is string {
  return (
    typeof value === 'string' &&
    value.trim().length > 0 &&
    value.length <= limit &&
    !value.includes('\0')
  );
}

function parseEdit(body: unknown): ReviewChange {
  const input = object(body);
  if (
    !Object.keys(input).length ||
    Object.keys(input).some((key) => key !== 'owner' && key !== 'due_date')
  ) {
    throw new ReviewError(422, 'Provide owner or due_date');
  }
  const change: Extract<ReviewChange, { action: 'edit' }> = { action: 'edit' };
  if ('owner' in input) {
    const owner = object(input.owner);
    const { department, role } = owner;
    if (
      Object.keys(owner).some((key) => key !== 'department' && key !== 'role') ||
      (department === undefined && role === undefined) ||
      (department !== undefined && !nonblank(department, 256)) ||
      (role !== undefined && !nonblank(role, 256))
    ) {
      throw new ReviewError(422, 'Provide a configured owner department or role');
    }
    change.owner = {
      ...(department === undefined ? {} : { department }),
      ...(role === undefined ? {} : { role }),
    };
  }
  if ('due_date' in input) {
    const date = input.due_date;
    if (date !== null && (typeof date !== 'string' || !isCalendarDate(date))) {
      throw new ReviewError(422, 'due_date must be YYYY-MM-DD or null');
    }
    change.dueDate = date;
  }
  return change;
}

function handle(run: (request: Request, response: Response) => Promise<void>): RequestHandler {
  return (request, response, next) => {
    response.set('Cache-Control', 'no-store');
    void run(request, response).catch((error: unknown) => {
      if (error instanceof ReviewError)
        response.status(error.status).json({ error: error.message });
      else next(error);
    });
  };
}

export function reviewRouter(repository: ObligationReviewRepository, guard: Guard): Router {
  const router = Router();
  router.get(
    '/api/obligations/review',
    guard('obligations:review'),
    handle(async (request, response) => {
      const { doc_id } = request.query;
      if (
        Object.keys(request.query).some((key) => key !== 'doc_id') ||
        (doc_id !== undefined && !nonblank(doc_id, 255))
      ) {
        throw new ReviewError(422, 'Invalid review filter');
      }
      response.json({ obligations: await repository.list(principal(response).orgId, doc_id) });
    }),
  );

  function change(parse: (body: unknown) => ReviewChange) {
    return handle(async (request, response) => {
      const { id, obligation_id } = request.params;
      if (!nonblank(id, 255) || !nonblank(obligation_id, 512)) {
        throw new ReviewError(422, 'Invalid document or obligation ID');
      }
      const obligation = await repository.change(
        id,
        obligation_id,
        parse(request.body),
        auditContext(request, response, principal(response)),
      );
      response.json({ obligation });
    });
  }

  const resource = '/api/documents/:id/obligations/:obligation_id/review';
  router.patch(resource, guard('obligations:review'), change(parseEdit));
  router.post(
    resource + '/approve',
    guard('obligations:review'),
    change(() => ({ action: 'approve' })),
  );
  router.post(
    resource + '/reject',
    guard('obligations:review'),
    change((body) => {
      const { reason } = object(body);
      if (!nonblank(reason, 2000))
        throw new ReviewError(422, 'Rejection requires a reason of up to 2000 characters');
      return { action: 'reject', reason: reason.trim() };
    }),
  );
  return router;
}
