import { readFileSync } from 'node:fs';

import request from 'supertest';
import { describe, expect, it, vi } from 'vitest';

import { scopedApp } from './scoped-app.js';
import { IntelligenceError } from '../src/intelligence/client.js';
import type { IntelligenceClient } from '../src/intelligence/client.js';

const example = JSON.parse(
  readFileSync(
    new URL('../../../contracts/examples/search-response.v1.json', import.meta.url),
    'utf8',
  ),
) as unknown;

function appWithSearch(search: IntelligenceClient['search']) {
  return scopedApp({
    intelligence: { search, ingest: vi.fn(), index: vi.fn() },
    documents: { save: vi.fn(), setStatus: vi.fn() },
  });
}

describe('GET /api/search', () => {
  it('forwards a typed request with flat filters and the debug flag', async () => {
    const search = vi.fn().mockResolvedValue(example);
    const response = await request(appWithSearch(search)).get('/api/search').query({
      query: 'विभाग report',
      top_k: 3,
      department: 'legal',
      document_type: 'agreement',
      doc_date_from: '2026-11-01',
      debug: 'true',
    });
    expect(response.status).toBe(200);
    expect(response.body).toEqual(example);
    expect(search).toHaveBeenCalledTimes(1);
    expect(search).toHaveBeenCalledWith({
      query: 'विभाग report',
      top_k: 3,
      debug: true,
      filters: {
        org_id: 'example',
        department: 'legal',
        document_type: 'agreement',
        doc_date_from: '2026-11-01',
      },
    });
  });

  it('forwards bracketed filters and only returns debug when requested', async () => {
    const search = vi.fn().mockResolvedValue(example);
    const response = await request(appWithSearch(search)).get(
      '/api/search?query=report&top_k=1&filters%5Blanguage%5D=hi&debug=false',
    );
    expect(response.status).toBe(200);
    expect(response.body).not.toHaveProperty('debug');
    expect(search).toHaveBeenCalledTimes(1);
    expect(search).toHaveBeenCalledWith({
      query: 'report',
      top_k: 1,
      debug: false,
      filters: { language: 'hi', org_id: 'example' },
    });
  });

  it('returns a clear 422 for blank queries before contacting intelligence', async () => {
    const search = vi.fn();
    const response = await request(appWithSearch(search))
      .get('/api/search')
      .query({ query: '  ', top_k: 3 });
    expect(response.status).toBe(422);
    expect(response.body).toEqual({ error: 'query must contain non-whitespace text' });
    expect(search).not.toHaveBeenCalled();
  });

  it.each([
    ['out-of-range top_k', 'query=report&top_k=101'],
    ['repeated filters', 'query=report&top_k=2&department=legal&department=finance'],
    ['invalid debug', 'query=report&top_k=2&debug=yes'],
  ])('rejects %s before calling intelligence', async (_label, queryString) => {
    const search = vi.fn();
    const response = await request(appWithSearch(search)).get(`/api/search?${queryString}`);
    expect(response.status).toBe(422);
    expect(search).not.toHaveBeenCalled();
  });

  it('rejects an upstream response with an invalid nested citation', async () => {
    const fixture = structuredClone(example) as {
      results: { citations: { page_start: number }[] }[];
    };
    const citation = fixture.results[0]?.citations[0];
    if (!citation) throw new Error('Expected an example citation');
    citation.page_start = 0;
    const search = vi.fn().mockResolvedValue(fixture);
    const response = await request(appWithSearch(search)).get('/api/search?query=report&top_k=3');
    expect(response.status).toBe(502);
    expect(response.body).toEqual({ error: 'Invalid intelligence search response' });
  });

  it.each([
    [422, 422],
    [503, 502],
  ])('maps upstream search error %i to %i', async (upstream, status) => {
    const search = vi.fn().mockRejectedValue(new IntelligenceError(upstream));
    const response = await request(appWithSearch(search)).get('/api/search?query=report&top_k=3');
    expect(response.status).toBe(status);
  });
});
