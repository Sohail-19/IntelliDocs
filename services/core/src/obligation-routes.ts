import { readFileSync } from 'node:fs';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { Router } from 'express';

import type { ExtractionResult, IntelligenceClient, Obligation } from './intelligence/client.js';
import { extractionFailure, extractionStatuses } from './obligations.js';
import type {
  ExtractedObligation,
  ObligationFilters,
  ObligationRepository,
} from './obligations.js';

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

export function runExtraction(
  repository: ObligationRepository,
  extract: IntelligenceClient['extractObligations'],
): (id: string) => Promise<ExtractionResult> {
  return async (id) => {
    const state = await repository.documentState(id);
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
          (item) => item.doc_id !== id || item.citations.some((citation) => citation.doc_id !== id),
        )
      ) {
        throw new Error('Invalid obligation document or status');
      }
      await repository.replace({ ...result, obligations });
      return result;
    } catch {
      await repository.recordFailure(id);
      throw new ExtractionRouteError(502, extractionFailure);
    }
  };
}

function calendarDate(value: string): boolean {
  return (
    /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    !value.startsWith('0000') &&
    Number.isFinite(Date.parse(value)) &&
    new Date(value).toISOString().slice(0, 10) === value
  );
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
        if (!calendarDate(value)) {
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
): Router {
  const router = Router();
  router.get('/api/obligations', (request, response, next) => {
    void (async () => {
      response.json({ obligations: await repository.list(parseFilters(request.query)) });
    })().catch((error: unknown) => {
      if (error instanceof ExtractionRouteError)
        response.status(error.status).json({ error: error.message });
      else next(error);
    });
  });
  router.get('/api/documents/:id/obligations', (request, response, next) => {
    void (async () => {
      const state = await repository.documentState(request.params.id);
      if (!state) {
        response.status(404).json({ error: 'Document not found' });
        return;
      }
      const obligations = await repository.list({ doc_id: request.params.id });
      response.json({
        obligations,
        extraction_status: state.extraction_status,
        extraction_error: state.extraction_error,
      });
    })().catch(next);
  });
  router.post('/api/documents/:id/obligations', (request, response, next) => {
    void (async () => {
      response.json(await run(request.params.id));
    })().catch((error: unknown) => {
      if (error instanceof ExtractionRouteError)
        response.status(error.status).json({ error: error.message });
      else next(error);
    });
  });
  return router;
}
