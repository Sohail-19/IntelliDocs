import { readFileSync } from 'node:fs';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { Router } from 'express';

import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import { IntelligenceError } from './intelligence/client.js';
import type { Answer, AnswerRequest, IntelligenceClient } from './intelligence/client.js';
import { authorityWarnings } from './relations/staleness.js';
import type { PostgresRelationRepository } from './relations/repository.js';

const ajv = new Ajv2020({ strict: true });
addFormats.default(ajv);
for (const name of ['citation', 'claim', 'answer', 'answer-request']) {
  const path = new URL(`../../../contracts/schemas/${name}.v1.schema.json`, import.meta.url);
  ajv.addSchema(JSON.parse(readFileSync(path, 'utf8')) as object);
}
const validateRequest = ajv.compile<AnswerRequest>({ $ref: 'answer-request.v1.schema.json' });
const validateResponse = ajv.compile<Answer>({ $ref: 'answer.v1.schema.json' });

export function answerRouter(
  answer: IntelligenceClient['answer'],
  guard: Guard,
  relations?: Pick<PostgresRelationRepository, 'confirmedChanges'>,
): Router {
  const router = Router();
  router.get('/api/answer', guard('documents:answer'), (request, response) => {
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
        const actor = principal(response);
        const result: unknown = await answer({
          ...body,
          filters: { ...body.filters, org_id: actor.orgSlug },
        });
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
        const citations = result.claims.flatMap((claim) => claim.citations);
        const changes = (await relations?.confirmedChanges(actor.orgId, citations)) ?? [];
        const warnings = authorityWarnings(changes, citations);
        response.set('Cache-Control', 'no-store');
        if (!warnings.length) {
          response.json(result);
          return;
        }
        // An answer sentence can combine several sources. Discard the claim
        // if any of its cited evidence is stale.
        const claims = result.claims.filter(
          (claim) => !authorityWarnings(changes, claim.citations).length,
        );
        response.json({
          ...result,
          claims,
          answer_text: claims.map((claim) => claim.text).join('\n'),
          abstained: claims.length === 0,
          abstain_reason: claims.length
            ? ''
            : 'The available answer relied on instructions changed by a newer document.',
          stale_sources: true,
          authority_warnings: warnings,
        });
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
