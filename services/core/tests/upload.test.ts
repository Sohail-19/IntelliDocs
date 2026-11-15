import type { Pool } from 'pg';
import request from 'supertest';
import { describe, expect, it, vi } from 'vitest';

import { createApp } from '../src/app.js';
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
    const app = createApp({ intelligence: { ingest }, documents: { save } });
    const result = await request(app)
      .post('/v1/upload')
      .field('org_id', 'example')
      .attach('file', Buffer.from('original message'), {
        filename: 'mail.eml',
        contentType: 'message/rfc822',
      });
    expect(result.status).toBe(202);
    expect(result.body).toEqual(response);
    const call = ingest.mock.calls[0] as [{ file: Blob; org_id: string }, string];
    expect(call[1]).toBe('mail.eml');
    expect(call[0].org_id).toBe('example');
    expect(await call[0].file.text()).toBe('original message');
    expect(save).toHaveBeenCalledWith(response);
  });

  it('rejects oversized files before calling intelligence', async () => {
    const ingest = vi.fn();
    const app = createApp({ intelligence: { ingest }, documents: { save: vi.fn() }, maxBytes: 4 });
    const result = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('too large'), 'input.pdf');
    expect(result.status).toBe(413);
    expect(ingest).not.toHaveBeenCalled();
  });

  it('does not persist when intelligence rejects the upload', async () => {
    const save = vi.fn();
    const app = createApp({
      intelligence: { ingest: vi.fn().mockRejectedValue(new IntelligenceError(415)) },
      documents: { save },
    });
    const result = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('bad'), 'bad.exe');
    expect(result.status).toBe(415);
    expect(save).not.toHaveBeenCalled();
  });

  it('does not report success when Postgres persistence fails', async () => {
    const app = createApp({
      intelligence: { ingest: vi.fn().mockResolvedValue(response) },
      documents: { save: vi.fn().mockRejectedValue(new Error('database unavailable')) },
    });
    const result = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('mail'), 'mail.eml');
    expect(result.status).toBe(500);
  });
});

describe('document repository', () => {
  it('stores parent and attachments in one transaction', async () => {
    const connection = {
      query: vi.fn().mockResolvedValue({ rows: [{ id: 42 }] }),
      release: vi.fn(),
    };
    const pool = { connect: vi.fn().mockResolvedValue(connection) } as unknown as Pool;
    await new PostgresDocumentRepository(pool).save(response);
    expect(connection.query).toHaveBeenNthCalledWith(1, 'BEGIN');
    expect(connection.query).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining('INSERT INTO documents'),
      ['parent', 'mail.eml', 'ready', null],
    );
    expect(connection.query).toHaveBeenNthCalledWith(
      3,
      expect.stringContaining('INSERT INTO documents'),
      ['child', 'attached.pdf', 'ready', 42],
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
    await expect(new PostgresDocumentRepository(pool).save(response)).rejects.toThrow(
      'insert failed',
    );
    expect(query).toHaveBeenLastCalledWith('ROLLBACK');
    expect(connection.release).toHaveBeenCalled();
  });
});
