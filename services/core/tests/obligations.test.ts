import type { Pool } from 'pg';
import request from 'supertest';
import { describe, expect, it, vi } from 'vitest';

import { fixtureAudit, scopedApp } from './scoped-app.js';
import type { IngestResponse } from '../src/intelligence/client.js';
import { PostgresObligationRepository } from '../src/obligations.js';
import type {
  DocumentExtractionResult,
  DocumentExtractionState,
  ObligationFilters,
  ObligationRepository,
} from '../src/obligations.js';

const indexed: DocumentExtractionState = {
  status: 'indexed',
  extraction_status: 'completed',
  extraction_error: null,
};

const extraction: DocumentExtractionResult = {
  doc_id: 'doc-a',
  obligations: [
    {
      id: 'obligation-a',
      doc_id: 'doc-a',
      action: 'File the report',
      owner: {
        raw_text: 'Finance',
        resolved_department: 'finance',
        resolved_role: null,
        resolved: true,
      },
      deadline: {
        raw_text: '5 December',
        normalized_date: '2026-12-05',
        is_relative: false,
        anchor_date: null,
        resolved: true,
      },
      citations: [{ doc_id: 'doc-a', page: 1, char_start: 0, char_end: 4, quote: 'File' }],
      obligation_type: 'reporting',
      confidence: 0,
      status: 'verified',
      extractor_version: 'test',
    },
  ],
  warnings: [],
  stats: { total: 1, extracted: 0, verified: 1, needs_review: 0, rejected: 0 },
};

const ingested: IngestResponse = {
  document_id: 'doc-a',
  parse_result: {
    document: {
      id: 'doc-a',
      filename: 'policy.pdf',
      mime_type: 'application/pdf',
      source_uri: 'file:///policy.pdf',
      status: 'ready',
      pages: [],
    },
    parser_name: 'test',
    parser_version: '1',
    warnings: [],
  },
};

function setup() {
  const repository = {
    documentState: vi.fn<ObligationRepository['documentState']>().mockResolvedValue(indexed),
    replace: vi.fn<ObligationRepository['replace']>().mockResolvedValue(undefined),
    recordFailure: vi.fn<ObligationRepository['recordFailure']>().mockResolvedValue(undefined),
    mutate: vi.fn<ObligationRepository['mutate']>().mockResolvedValue('updated'),
    list: vi.fn<ObligationRepository['list']>().mockResolvedValue(extraction.obligations),
  };
  const index = vi
    .fn()
    .mockResolvedValue({ doc_id: 'doc-a', chunk_count: 1, embedding_model: 'fake', warnings: [] });
  const extractObligations = vi.fn().mockResolvedValue(extraction);
  const setStatus = vi.fn().mockResolvedValue(undefined);
  const app = scopedApp({
    intelligence: { ingest: vi.fn().mockResolvedValue(ingested), index, extractObligations },
    documents: { save: vi.fn().mockResolvedValue(undefined), setStatus },
    obligations: repository,
  });
  return { app, repository, index, extractObligations, setStatus };
}

describe('obligation routes', () => {
  it('runs extraction after indexed state is recorded and permits a retry', async () => {
    const { app, repository, extractObligations, setStatus } = setup();
    const uploaded = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('PDF'), 'policy.pdf');
    expect(uploaded.status).toBe(202);
    expect(uploaded.body).toMatchObject({ extraction_results: [extraction] });
    expect(setStatus).toHaveBeenCalledWith(
      'doc-a',
      expect.objectContaining({ orgId: 42 }),
      'indexed',
    );
    expect(setStatus.mock.invocationCallOrder[0]).toBeLessThan(
      extractObligations.mock.invocationCallOrder[0] ?? 0,
    );
    expect(extractObligations).toHaveBeenCalledWith({ doc_id: 'doc-a' });
    expect(repository.replace).toHaveBeenCalledWith(
      extraction,
      expect.objectContaining({ orgId: 42 }),
    );
    expect((await request(app).post('/api/documents/doc-a/obligations')).status).toBe(200);
    expect(repository.replace).toHaveBeenCalledTimes(2);
  });

  it('records extraction failure separately and skips extraction after failed indexing', async () => {
    const { app, repository, index, extractObligations, setStatus } = setup();
    extractObligations.mockRejectedValueOnce(new Error('upstream unavailable'));
    const failed = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('PDF'), 'policy.pdf');
    expect(failed.status).toBe(502);
    expect(failed.body).toMatchObject({ extraction_failed_document_ids: ['doc-a'] });
    expect(setStatus).toHaveBeenCalledTimes(1);
    expect(setStatus).toHaveBeenCalledWith(
      'doc-a',
      expect.objectContaining({ orgId: 42 }),
      'indexed',
    );
    expect(repository.recordFailure).toHaveBeenCalledWith(
      'doc-a',
      expect.objectContaining({ orgId: 42 }),
    );
    expect(repository.replace).not.toHaveBeenCalled();
    index.mockRejectedValueOnce(new Error('index unavailable'));
    const unindexed = await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('PDF'), 'policy.pdf');
    expect(unindexed.status).toBe(502);
    expect(extractObligations).toHaveBeenCalledTimes(1);
  });

  it('rejects extraction before indexing and rejects a response for another document', async () => {
    const { app, repository, extractObligations } = setup();
    repository.documentState.mockResolvedValueOnce({ ...indexed, status: 'parsed' });
    expect((await request(app).post('/api/documents/doc-a/obligations')).status).toBe(409);
    expect(extractObligations).not.toHaveBeenCalled();
    extractObligations.mockResolvedValueOnce({ ...extraction, doc_id: 'wrong' });
    expect((await request(app).post('/api/documents/doc-a/obligations')).status).toBe(502);
    expect(repository.recordFailure).toHaveBeenCalledWith(
      'doc-a',
      expect.objectContaining({ orgId: 42 }),
    );
    expect(repository.replace).not.toHaveBeenCalled();
  });

  it('passes typed filters and includes extraction state in the document view', async () => {
    const { app, repository } = setup();
    const filters: ObligationFilters = {
      doc_id: 'doc-a',
      status: 'verified',
      owner_department: 'finance',
      due_before: '2026-12-06',
      due_after: '2026-12-04',
    };
    const all = await request(app).get('/api/obligations').query(filters);
    expect(all.status).toBe(200);
    expect(all.body).toEqual({ obligations: extraction.obligations });
    expect(repository.list).toHaveBeenCalledWith(filters, 42);
    const document = await request(app).get('/api/documents/doc-a/obligations');
    expect(document.body).toEqual({
      obligations: extraction.obligations,
      extraction_status: 'completed',
      extraction_error: null,
    });
    expect(repository.list).toHaveBeenLastCalledWith({ doc_id: 'doc-a' }, 42);
    repository.documentState.mockResolvedValueOnce(undefined);
    expect((await request(app).get('/api/documents/missing/obligations')).status).toBe(404);
  });

  it('rejects a score inconsistent with its evidence before storing or routing it', async () => {
    const { app, repository, extractObligations } = setup();
    extractObligations.mockResolvedValueOnce({
      ...extraction,
      obligations: extraction.obligations.map((item) => ({
        ...item,
        confidence: 0.9,
        confidence_breakdown: {
          quote_exact: 0.25,
          quote_fuzzy: 0,
          owner_resolved: 0,
          deadline_resolved: 0,
          recognized_cue: 0,
          cross_chunk_agreement: 0,
        },
      })),
    });
    expect((await request(app).post('/api/documents/doc-a/obligations')).status).toBe(502);
    expect(repository.replace).not.toHaveBeenCalled();
    expect(repository.recordFailure).toHaveBeenCalled();
  });

  it.each(['due_after=2026-02-30', 'status=made_up', 'doc_id=a&doc_id=b'])(
    'rejects invalid filters (%s)',
    async (query) => {
      const { app, repository } = setup();
      expect((await request(app).get('/api/obligations?' + query)).status).toBe(422);
      expect(repository.list).not.toHaveBeenCalled();
    },
  );
});

describe('obligation repository', () => {
  it('replaces the previous snapshot and records its audit in one transaction', async () => {
    const query = vi
      .fn<(statement: string, parameters?: unknown[]) => Promise<unknown>>()

      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [{ id: 10 }] })
      .mockResolvedValueOnce({ rows: [] })
      .mockResolvedValueOnce({ rows: [] })
      .mockResolvedValueOnce({ rows: [{ id: 100, assigned_user_id: null }] })
      .mockResolvedValue({});
    const connection = { query, release: vi.fn() };
    const pool = { connect: vi.fn().mockResolvedValue(connection) } as unknown as Pool;
    const repository = new PostgresObligationRepository(pool);
    await repository.replace(extraction, fixtureAudit);
    expect(query.mock.calls.map(([statement]) => statement)).toEqual([
      'BEGIN',
      expect.stringContaining('SELECT d.id, d.extraction_status'),
      expect.stringContaining('SELECT id AS row_id'),
      expect.stringContaining('FROM document_relations'),
      expect.stringContaining('INSERT INTO obligations'),
      expect.stringContaining('UPDATE documents'),
      expect.stringContaining('INSERT INTO audit_log'),
      'COMMIT',
    ]);
    expect(connection.release).toHaveBeenCalled();
  });

  it('rolls back replacement if storing part of the snapshot fails', async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [{ id: 10 }] })
      .mockResolvedValueOnce({ rows: [] })
      .mockResolvedValueOnce({ rows: [] })
      .mockRejectedValueOnce(new Error('insert failed'));
    const connection = { query, release: vi.fn() };
    const repository = new PostgresObligationRepository({
      connect: vi.fn().mockResolvedValue(connection),
    } as unknown as Pool);
    await expect(repository.replace(extraction, fixtureAudit)).rejects.toThrow('insert failed');
    expect(query).toHaveBeenLastCalledWith('ROLLBACK');
    expect(connection.release).toHaveBeenCalled();
  });
});
