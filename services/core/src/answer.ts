import { readFileSync } from 'node:fs';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { Router } from 'express';

import { IntelligenceError } from './intelligence/client.js';
import type { Answer, AnswerRequest, IntelligenceClient } from './intelligence/client.js';

const ajv = new Ajv2020({ strict: true });
addFormats.default(ajv);
for (const name of ['citation', 'claim', 'answer', 'answer-request']) {
  const path = new URL(`../../../contracts/schemas/${name}.v1.schema.json`, import.meta.url);
  ajv.addSchema(JSON.parse(readFileSync(path, 'utf8')) as object);
}
const validateRequest = ajv.compile<AnswerRequest>({ $ref: 'answer-request.v1.schema.json' });
const validateResponse = ajv.compile<Answer>({ $ref: 'answer.v1.schema.json' });

export function answerRouter(answer: IntelligenceClient['answer']): Router {
  const router = Router();
  router.get('/api/answer', (request, response) => {
    const { query, language, top_k, filters, ...flatFilters } = request.query;
    if (typeof query !== 'string' || !query.trim()) {
      response.status(422).json({ error: 'query must contain non-whitespace text' });
      return;
    }
    if (typeof language !== 'string' || language.trim().length < 2) {
      response.status(422).json({ error: 'language must be an explicit language tag' });
      return;
    }
    if (typeof top_k !== 'string' || !/^\d+$/.test(top_k)) {
      response.status(422).json({ error: 'top_k must be an integer between 1 and 100' });
      return;
    }
    const hasFlatFilters = Object.keys(flatFilters).length > 0;
    if (filters !== undefined && hasFlatFilters) {
      response.status(422).json({ error: 'Use either flat filters or filters[field] parameters' });
      return;
    }
    const body: unknown = {
      query,
      language,
      top_k: Number(top_k),
      ...(filters !== undefined || hasFlatFilters ? { filters: filters ?? flatFilters } : {}),
    };
    if (!validateRequest(body)) {
      response
        .status(422)
        .json({ error: 'Invalid top_k or filters; expected string filter values' });
      return;
    }
    void (async () => {
      try {
        const result: unknown = await answer(body);
        if (
          !validateResponse(result) ||
          result.claims.some(
            (claim) => claim.status !== 'verified' || claim.citations.length === 0,
          ) ||
          (result.abstained
            ? result.claims.length !== 0 || result.answer_text !== '' || !result.abstain_reason
            : result.claims.length === 0 ||
              result.answer_text !== result.claims.map((claim) => claim.text).join('\n'))
        ) {
          response.status(502).json({ error: 'Invalid intelligence answer response' });
          return;
        }
        response.json(result);
      } catch (error) {
        if (error instanceof IntelligenceError && error.status === 422) {
          response.status(422).json({ error: 'Intelligence rejected the answer query or filters' });
          return;
        }
        response.status(502).json({ error: 'Intelligence answer failed' });
      }
    })();
  });
  return router;
}
