import type { Pool } from 'pg';
import request from 'supertest';
import { describe, expect, it, vi } from 'vitest';

import { fixtureAudit, scopedApp } from './scoped-app.js';
import { PostgresDocumentRepository } from '../src/documents.js';
import { IntelligenceError } from '../src/intelligence/client.js';
import type { IngestResponse } from '../src/intelligence/client.js';

const response: IngestResponse = {
  document_id: 'parent',
  parse_result: {
    document: {
      id: 'parent',
      filename: 'mail.eml',
      mime_type: 'message/rfc822',
      source_uri: 'file:///storage/parent',
      status: 'ready',
      pages: [],
    },
    parser_name: 'test',
    parser_version: '1',
    warnings: [],
    attachments: [
      {
        document: {
          id: 'child',
          parent_document_id: 'parent',
          filename: 'attached.pdf',
          mime_type: 'application/pdf',
          source_uri: 'file:///storage/child',
          status: 'ready',
          pages: [],
        },
        parser_name: 'test',
        parser_version: '1',
        warnings: [],
      },
    ],
  },
};

describe('core upload flow', () => {
  it('forwards file and org through the typed client, then persists the result', async () => {
    const ingest = vi.fn().mockResolvedValue(response);
    const save = vi.fn().mockResolvedValue(undefined);
    const index = vi.fn().mockImplementation((request: { doc_id: string }) =>
      Promise.resolve({
        doc_id: request.doc_id,
        chunk_count: 1,
        embedding_model: 'fake',
        warnings: [],
      }),
    );
    const setStatus = vi.fn().mockResolvedValue(undefined);
    const app = scopedApp({ intelligence: { ingest, index }, documents: { save, setStatus } });
    const result = await request(app)
      .post('/v1/upload')
      .field('org_id', 'example')
      .attach('file', Buffer.from('original message'), {
        filename: 'mail.eml',
        contentType: 'message/rfc822',
      });
    expect(result.status).toBe(202);
    expect(result.body).toMatchObject(response);
    const call = ingest.mock.calls[0] as [{ file: Blob; org_id: string }, string];
    expect(call[1]).toBe('mail.eml');
    expect(call[0].org_id).toBe('example');
    expect(await call[0].file.text()).toBe('original message');
    expect(save).toHaveBeenCalledWith(response, expect.objectContaining({ orgId: 42 }));
    expect((ingest.mock.calls[0] as [{ index: boolean }])[0].index).toBe(false);
    expect(index).toHaveBeenNthCalledWith(1, { doc_id: 'parent' });
    expect(index).toHaveBeenNthCalledWith(2, { doc_id: 'child' });
    expect(setStatus).toHaveBeenCalledWith(
      'parent',
      expect.objectContaining({ orgId: 42 }),
      'indexed',
    );
    expect(setStatus).toHaveBeenCalledWith(
      'child',
      expect.objectContaining({ orgId: 42 }),
      'indexed',
    );
    expect(save.mock.invocationCallOrder[0]).toBeLessThan(
      index.mock.invocationCallOrder[0] ?? Infinity,
    );
  });

  it('rejects oversized files before calling intelligence', async () => {
    const ingest = vi.fn();
    const app = scopedApp({
      intelligence: { ingest, index: vi.fn() },
      documents: { save: vi.fn(), setStatus: vi.fn() },
      maxBytes: 4,
    });
    const result = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('too large'), 'input.pdf');
    expect(result.status).toBe(413);
    expect(ingest).not.toHaveBeenCalled();
  });

  it('does not persist when intelligence rejects the upload', async () => {
    const save = vi.fn();
    const app = scopedApp({
      intelligence: {
        ingest: vi.fn().mockRejectedValue(new IntelligenceError(415)),
        index: vi.fn(),
      },
      documents: { save, setStatus: vi.fn() },
    });
    const result = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('bad'), 'bad.exe');
    expect(result.status).toBe(415);
    expect(save).not.toHaveBeenCalled();
  });

  it('does not report success when Postgres persistence fails', async () => {
    const app = scopedApp({
      intelligence: { ingest: vi.fn().mockResolvedValue(response), index: vi.fn() },
      documents: {
        save: vi.fn().mockRejectedValue(new Error('database unavailable')),
        setStatus: vi.fn(),
      },
    });
    const result = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('mail'), 'mail.eml');
    expect(result.status).toBe(500);
  });

  it('records failed indexing and returns retryable document ids', async () => {
    const setStatus = vi.fn().mockResolvedValue(undefined);
    const save = vi.fn().mockResolvedValue(undefined);
    const index = vi.fn().mockRejectedValue(new IntelligenceError(503));
    const app = scopedApp({
      intelligence: { ingest: vi.fn().mockResolvedValue(response), index },
      documents: { save, setStatus },
    });
    const result = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('mail'), 'mail.eml');
    expect(result.status).toBe(502);
    const body = result.body as { document_id: string; index_error?: string };
    expect(body.document_id).toBe('parent');
    expect(typeof body.index_error).toBe('string');
    expect(setStatus).toHaveBeenCalledWith(
      'parent',
      expect.objectContaining({ orgId: 42 }),
      'failed',
      'Indexing failed',
    );
    expect(setStatus).toHaveBeenCalledWith(
      'child',
      expect.objectContaining({ orgId: 42 }),
      'failed',
      'Indexing failed',
    );
    expect(save).toHaveBeenCalled();
  });

  it('forwards explicit metadata without guessing dates', async () => {
    const ingest = vi.fn().mockResolvedValue(response);
    const app = scopedApp({
      intelligence: {
        ingest,
        index: vi.fn().mockImplementation((request: { doc_id: string }) =>
          Promise.resolve({
            doc_id: request.doc_id,
            chunk_count: 1,
            embedding_model: 'fake',
            warnings: [],
          }),
        ),
      },
      documents: {
        save: vi.fn().mockResolvedValue(undefined),
        setStatus: vi.fn().mockResolvedValue(undefined),
      },
    });
    const result = await request(app)
      .post('/v1/upload')
      .field('department', 'legal')
      .field('doc_type', 'agreement')
      .field('doc_date', '2026-11-15')
      .attach('file', Buffer.from('mail'), 'mail.eml');
    expect(result.status).toBe(202);
    expect(ingest).toHaveBeenCalledWith(
      expect.objectContaining({
        department: 'legal',
        doc_type: 'agreement',
        doc_date: '2026-11-15',
        index: false,
      }),
      'mail.eml',
    );
  });
});

describe('document repository', () => {
  it('updates indexing status and rejects a missing row', async () => {
    const before = { status: 'parsed', index_error: null, indexed_at: null };
    const after = { status: 'indexed', index_error: null, indexed_at: new Date('2026-01-01') };
    const query = vi
      .fn()
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [before] })
      .mockResolvedValueOnce({ rows: [after] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [] })
      .mockResolvedValueOnce({});
    const connection = { query, release: vi.fn() };
    const pool = { connect: vi.fn().mockResolvedValue(connection) } as unknown as Pool;
    const repository = new PostgresDocumentRepository(pool);
    await repository.setStatus('parent', fixtureAudit, 'indexed');
    expect(query).toHaveBeenCalledWith(expect.stringContaining('UPDATE documents'), [
      'parent',
      'indexed',
      null,
      42,
    ]);
    await expect(repository.setStatus('missing', fixtureAudit, 'failed', 'failed')).rejects.toThrow(
      'no matching row',
    );
  });
  it('stores parent and attachments in one transaction', async () => {
    const connection = {
      query: vi.fn().mockResolvedValue({ rows: [{ id: 42 }] }),
      release: vi.fn(),
    };
    const pool = { connect: vi.fn().mockResolvedValue(connection) } as unknown as Pool;
    await new PostgresDocumentRepository(pool).save(response, fixtureAudit);
    expect(connection.query).toHaveBeenNthCalledWith(1, 'BEGIN');
    expect(connection.query).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining('INSERT INTO documents'),
      ['parent', 'mail.eml', 'parsed', null, null, 42, null, null, null, 'message/rfc822', [], 0],
    );
    expect(connection.query).toHaveBeenNthCalledWith(
      4,
      expect.stringContaining('INSERT INTO documents'),
      ['child', 'attached.pdf', 'parsed', 42, null, 42, null, null, null, 'application/pdf', [], 0],
    );
    expect(connection.query).toHaveBeenLastCalledWith('COMMIT');
    expect(connection.release).toHaveBeenCalled();
  });

  it('rolls back failed inserts and releases the connection', async () => {
    const query = vi
      .fn()
      .mockResolvedValue({ rows: [] })
      .mockResolvedValueOnce({ rows: [] })
      .mockRejectedValueOnce(new Error('insert failed'));
    const connection = { query, release: vi.fn() };
    const pool = { connect: vi.fn().mockResolvedValue(connection) } as unknown as Pool;
    await expect(new PostgresDocumentRepository(pool).save(response, fixtureAudit)).rejects.toThrow(
      'insert failed',
    );
    expect(query).toHaveBeenLastCalledWith('ROLLBACK');
    expect(connection.release).toHaveBeenCalled();
  });
});
