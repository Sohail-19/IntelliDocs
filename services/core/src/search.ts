import { readFileSync } from 'node:fs';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { Router } from 'express';

import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import { IntelligenceError } from './intelligence/client.js';
import type { IntelligenceClient, SearchRequest, SearchResponse } from './intelligence/client.js';

const ajv = new Ajv2020({ strict: true });
addFormats.default(ajv);
for (const name of ['chunk', 'citation', 'search-result', 'search-request', 'search-response']) {
  const path = new URL(`../../../contracts/schemas/${name}.v1.schema.json`, import.meta.url);
  ajv.addSchema(JSON.parse(readFileSync(path, 'utf8')) as object);
}
const validateRequest = ajv.compile<SearchRequest>({ $ref: 'search-request.v1.schema.json' });
const validateResponse = ajv.compile<SearchResponse>({ $ref: 'search-response.v1.schema.json' });

export function searchRouter(search: IntelligenceClient['search'], guard: Guard): Router {
  const router = Router();

  router.get('/api/search', guard('documents:search'), (request, response) => {
    const { query, top_k, debug, filters, ...flatFilters } = request.query;
    if (typeof query !== 'string' || !query.trim()) {
      response.status(422).json({ error: 'query must contain non-whitespace text' });
      return;
    }
    if (typeof top_k !== 'string' || !/^\d+$/.test(top_k)) {
      response.status(422).json({ error: 'top_k must be an integer between 1 and 100' });
      return;
    }
    if (debug !== undefined && debug !== 'true' && debug !== 'false') {
      response.status(422).json({ error: 'debug must be true or false' });
      return;
    }
    const hasFlatFilters = Object.keys(flatFilters).length > 0;
    if (filters !== undefined && hasFlatFilters) {
      response.status(422).json({ error: 'Use either flat filters or filters[field] parameters' });
      return;
    }
    const body: unknown = {
      query,
      top_k: Number(top_k),
      ...(debug !== undefined ? { debug: debug === 'true' } : {}),
      ...(filters !== undefined || hasFlatFilters ? { filters: filters ?? flatFilters } : {}),
    };
    if (!validateRequest(body)) {
      const issue = validateRequest.errors?.[0];
      const error =
        issue?.instancePath === '/top_k'
          ? 'top_k must be an integer between 1 and 100'
          : 'filters must be a map of string values';
      response.status(422).json({ error });
      return;
    }

    void (async () => {
      try {
        const result: unknown = await search({
          ...body,
          filters: { ...body.filters, org_id: principal(response).orgSlug },
        });
        if (!validateResponse(result)) {
          response.status(502).json({ error: 'Invalid intelligence search response' });
          return;
        }
        response.json(body.debug ? result : { results: result.results });
      } catch (error) {
        if (error instanceof IntelligenceError && error.status === 422) {
          response.status(422).json({ error: 'Intelligence rejected the search query or filters' });
          return;
        }
        response.status(502).json({ error: 'Intelligence search failed' });
      }
    })();
  });

  return router;
}
