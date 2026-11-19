import { readFileSync } from 'node:fs';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { Router } from 'express';

import { auditContext } from './audit.js';
import type { AuditContext } from './audit.js';
import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import type { ExtractionResult, IntelligenceClient, Obligation } from './intelligence/client.js';
import { extractionFailure, extractionStatuses } from './obligations.js';
import type {
  ExtractedObligation,
  ObligationFilters,
  ObligationMutation,
  ObligationRepository,
} from './obligations.js';
import { isCalendarDate } from './obligation-values.js';

const ajv = new Ajv2020({ strict: true });
addFormats.default(ajv);
for (const name of [
  'citation',
  'obligation-owner',
  'obligation-deadline',
  'obligation',
  'extraction-stats',
  'extraction-result',
]) {
  const path = new URL('../../../contracts/schemas/' + name + '.v1.schema.json', import.meta.url);
  ajv.addSchema(JSON.parse(readFileSync(path, 'utf8')) as object);
}
const validateResult = ajv.compile<ExtractionResult>({ $ref: 'extraction-result.v1.schema.json' });

export class ExtractionRouteError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function isExtractionStatus(status: string): status is ExtractedObligation['status'] {
  return extractionStatuses.some((value) => value === status);
}

function isExtracted(obligation: Obligation): obligation is ExtractedObligation {
  return isExtractionStatus(obligation.status);
}

function consistentConfidence(obligation: ExtractedObligation): boolean {
  const breakdown = obligation.confidence_breakdown;
  if (!breakdown) return true;
  const total = Object.values(breakdown).reduce((sum, contribution) => sum + contribution, 0);
  return (
    (breakdown.quote_exact === 0 || breakdown.quote_fuzzy === 0) &&
    Math.abs(total - obligation.confidence) <= 1e-12
  );
}

export function runExtraction(
  repository: ObligationRepository,
  extract: IntelligenceClient['extractObligations'],
): (id: string, audit: AuditContext) => Promise<ExtractionResult> {
  return async (id, audit) => {
    const state = await repository.documentState(id, audit.orgId);
    if (!state) throw new ExtractionRouteError(404, 'Document not found');
    if (state.status !== 'indexed') {
      throw new ExtractionRouteError(409, 'Document must be indexed before extraction');
    }
    try {
      const result: unknown = await extract({ doc_id: id });
      if (!validateResult(result) || result.doc_id !== id) {
        throw new Error('Invalid intelligence extraction response');
      }
      const obligations = result.obligations.filter(isExtracted);
      if (
        obligations.length !== result.obligations.length ||
        obligations.some(
          (item) =>
            item.doc_id !== id ||
            item.citations.some((citation) => citation.doc_id !== id) ||
            !consistentConfidence(item),
        )
      ) {
        throw new Error('Invalid obligation document, status, or confidence breakdown');
      }
      await repository.replace({ ...result, obligations }, audit);
      return result;
    } catch {
      await repository.recordFailure(id, audit);
      throw new ExtractionRouteError(502, extractionFailure);
    }
  };
}

function parseFilters(query: Record<string, unknown>): ObligationFilters {
  const filters: ObligationFilters = {};
  for (const [name, value] of Object.entries(query)) {
    if (typeof value !== 'string' || !value.trim()) {
      throw new ExtractionRouteError(422, 'Invalid ' + name);
    }
    switch (name) {
      case 'doc_id':
      case 'owner_department':
        filters[name] = value;
        break;
      case 'status':
        if (!isExtractionStatus(value)) throw new ExtractionRouteError(422, 'Invalid status');
        filters.status = value;
        break;
      case 'due_before':
      case 'due_after':
        if (!isCalendarDate(value)) {
          throw new ExtractionRouteError(422, name + ' must be a calendar date (YYYY-MM-DD)');
        }
        filters[name] = value;
        break;
      default:
        throw new ExtractionRouteError(422, 'Unknown obligation filter: ' + name);
    }
  }
  return filters;
}

export function obligationRouter(
  repository: ObligationRepository,
  run: ReturnType<typeof runExtraction>,
  guard: Guard,
): Router {
  const router = Router();
  router.get('/api/obligations', guard('obligations:read'), (request, response, next) => {
    void (async () => {
      response.json({
        obligations: await repository.list(parseFilters(request.query), principal(response).orgId),
      });
    })().catch((error: unknown) => {
      if (error instanceof ExtractionRouteError)
        response.status(error.status).json({ error: error.message });
      else next(error);
    });
  });
  router.get(
    '/api/documents/:id/obligations',
    guard('obligations:read'),
    (request, response, next) => {
      const id = request.params.id;
      if (typeof id !== 'string') {
        response.sendStatus(400);
        return;
      }
      void (async () => {
        const { orgId } = principal(response);
        const state = await repository.documentState(id, orgId);
        if (!state) {
          response.status(404).json({ error: 'Document not found' });
          return;
        }
        const obligations = await repository.list({ doc_id: id }, orgId);
        response.json({
          obligations,
          extraction_status: state.extraction_status,
          extraction_error: state.extraction_error,
        });
      })().catch(next);
    },
  );
  router.post(
    '/api/documents/:id/obligations',
    guard('obligations:extract'),
    (request, response, next) => {
      const id = request.params.id;
      if (typeof id !== 'string') {
        response.sendStatus(400);
        return;
      }
      void (async () => {
        response.json(await run(id, auditContext(request, response, principal(response))));
      })().catch((error: unknown) => {
        if (error instanceof ExtractionRouteError)
          response.status(error.status).json({ error: error.message });
        else next(error);
      });
    },
  );
  const resource = '/api/documents/:id/obligations/:obligation_id';
  for (const [field, permission] of [
    ['status', 'obligations:status'],
    ['assignment', 'obligations:assign'],
  ] as const) {
    router.patch(resource + '/' + field, guard(permission), (request, response, next) => {
      const { id, obligation_id } = request.params;
      const body: unknown = request.body;
      if (
        typeof id !== 'string' ||
        typeof obligation_id !== 'string' ||
        !body ||
        typeof body !== 'object' ||
        Array.isArray(body)
      ) {
        response.status(422).json({ error: 'Invalid obligation change' });
        return;
      }
      const { status, assigned_user_id } = body as Record<string, unknown>;
      let change: ObligationMutation;
      if (field === 'status' && typeof status === 'string' && isExtractionStatus(status)) {
        change = { status };
      } else if (
        field === 'assignment' &&
        (assigned_user_id === null ||
          (typeof assigned_user_id === 'number' &&
            Number.isInteger(assigned_user_id) &&
            assigned_user_id > 0 &&
            assigned_user_id <= 2147483647))
      ) {
        change = { assignedUserId: assigned_user_id };
      } else {
        response.status(422).json({ error: 'Invalid obligation change' });
        return;
      }
      void repository
        .mutate(id, obligation_id, change, auditContext(request, response, principal(response)))
        .then((outcome) => {
          switch (outcome) {
            case 'updated':
              response.sendStatus(204);
              break;
            case 'missing':
              response.status(404).json({ error: 'Obligation not found' });
              break;
            case 'invalid_assignee':
              response.status(422).json({ error: 'Invalid assignee' });
              break;
            case 'missing_citation':
              response.status(409).json({ error: 'Verification requires a citation' });
              break;
            case 'use_review':
              response.status(409).json({ error: 'Use the obligation review workflow' });
              break;
          }
        })
        .catch(next);
    });
  }
  return router;
}
