import { describe, expect, it } from 'vitest';

import { IntelligenceClient } from '../src/intelligence/client.js';

function mockFetch(requests: Array<{ url: string; init?: RequestInit }>): typeof fetch {
  return (input, init) => {
    const url = input instanceof URL ? input.href : input instanceof Request ? input.url : input;
    requests.push({ url, init });
    return Promise.resolve(
      new Response(JSON.stringify({ document_id: 'doc-001', results: [], obligations: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
  };
}

describe('intelligence client', () => {
  it('builds contract endpoint requests with the configured base URL', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const client = new IntelligenceClient('https://intelligence.example/', mockFetch(requests));

    await client.health();
    await client.ingest({ file: new Blob(['example']), org_id: 'example' }, 'input.pdf');
    await client.search({ query: 'deadline', top_k: 3 });
    await client.extractObligations({ doc_id: 'doc-001' });

    expect(requests).toHaveLength(4);
    expect(requests[0]).toMatchObject({ url: 'https://intelligence.example/health' });
    expect(requests[1]?.url).toBe('https://intelligence.example/v1/ingest');
    expect(requests[1]?.init?.method).toBe('POST');
    expect(requests[1]?.init?.headers).toBeUndefined();
    const body = requests[1]?.init?.body as FormData;
    expect(body.get('org_id')).toBe('example');
    const file = body.get('file') as File;
    expect(file.name).toBe('input.pdf');
    expect(await file.text()).toBe('example');
    expect(requests.slice(2)).toEqual([
      {
        url: 'https://intelligence.example/v1/search',
        init: {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ query: 'deadline', top_k: 3 }),
        },
      },
      {
        url: 'https://intelligence.example/v1/extract-obligations',
        init: {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ doc_id: 'doc-001' }),
        },
      },
    ]);
  });
});
