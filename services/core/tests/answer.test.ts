import { readFileSync } from 'node:fs';

import request from 'supertest';
import { describe, expect, it, vi } from 'vitest';

import { createApp } from '../src/app.js';
import { IntelligenceError } from '../src/intelligence/client.js';
import type { Answer, IntelligenceClient } from '../src/intelligence/client.js';

const example = JSON.parse(
  readFileSync(new URL('../../../contracts/examples/answer.v1.json', import.meta.url), 'utf8'),
) as Answer;

function appWithAnswer(answer: IntelligenceClient['answer']) {
  return createApp({
    intelligence: { answer, ingest: vi.fn(), index: vi.fn() },
    documents: { save: vi.fn(), setStatus: vi.fn() },
  });
}

describe('GET /api/answer', () => {
  it('forwards query, language, top_k and filters through the typed client', async () => {
    const answer = vi.fn().mockResolvedValue(example);
    const response = await request(appWithAnswer(answer)).get('/api/answer').query({
      query: 'रिपोर्ट कब देनी है?',
      language: 'hi',
      top_k: 3,
      department: 'records',
    });
    expect(response.status).toBe(200);
    expect(response.body).toEqual(example);
    expect(answer).toHaveBeenCalledTimes(1);
    expect(answer).toHaveBeenCalledWith({
      query: 'रिपोर्ट कब देनी है?',
      language: 'hi',
      top_k: 3,
      filters: { department: 'records' },
    });
  });

  it('supports bracketed filters and valid abstentions', async () => {
    const abstention = {
      ...example,
      answer_text: '',
      claims: [],
      abstained: true,
      abstain_reason: 'No evidence.',
    };
    const answer = vi.fn().mockResolvedValue(abstention);
    const response = await request(appWithAnswer(answer)).get(
      '/api/answer?query=report&language=en&top_k=2&filters%5Blanguage%5D=hi',
    );
    expect(response.status).toBe(200);
    expect(response.body).toEqual(abstention);
    expect(answer).toHaveBeenCalledWith({
      query: 'report',
      language: 'en',
      top_k: 2,
      filters: { language: 'hi' },
    });
  });

  it.each([
    'query=%20%20&language=en&top_k=2',
    'query=report&top_k=2',
    'query=report&language=e&top_k=2',
    'query=report&language=en&top_k=101',
    'query=report&language=en&top_k=1.2',
    'query=report&language=en&top_k=2&department=a&department=b',
    'query=report&language=en&top_k=2&department=a&filters[doc_id]=d',
  ])('rejects invalid parameters: %s', async (parameters) => {
    const answer = vi.fn();
    expect((await request(appWithAnswer(answer)).get(`/api/answer?${parameters}`)).status).toBe(
      422,
    );
    expect(answer).not.toHaveBeenCalled();
  });

  it.each(['unverified', 'invalid-citation', 'extra-text', 'false-abstention'])(
    'rejects unsafe upstream output: %s',
    async (kind) => {
      const value = structuredClone(example);
      const claim = value.claims[0];
      if (!claim) throw new Error('Expected example claim');
      if (kind === 'unverified') claim.status = 'unverified';
      if (kind === 'invalid-citation') claim.citations = [];
      if (kind === 'extra-text') value.answer_text += ' An unsupported addition.';
      if (kind === 'false-abstention') value.abstained = true;
      const answer = vi.fn().mockResolvedValue(value);
      const response = await request(appWithAnswer(answer)).get(
        '/api/answer?query=report&language=en&top_k=2',
      );
      expect(response.status).toBe(502);
    },
  );

  it.each([
    [422, 422],
    [503, 502],
  ])('maps upstream status %i to %i', async (upstream, expected) => {
    const answer = vi.fn().mockRejectedValue(new IntelligenceError(upstream));
    const response = await request(appWithAnswer(answer)).get(
      '/api/answer?query=report&language=en&top_k=2',
    );
    expect(response.status).toBe(expected);
  });
});
