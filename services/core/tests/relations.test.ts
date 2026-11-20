import { randomUUID } from 'node:crypto';

import pg from 'pg';
import type { Pool } from 'pg';
import request from 'supertest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Citation } from '../../../contracts/core-api.v1.js';
import { up as initial } from '../migrations/1700000000000_initial.js';
import { up as parents } from '../migrations/1700000000001_document_parents.js';
import { up as indexing } from '../migrations/1700000000002_index_status.js';
import { up as extraction } from '../migrations/1700000000003_obligation_extraction.js';
import { up as auth } from '../migrations/1700000000004_auth.js';
import { up as audit } from '../migrations/1700000000005_audit_trail.js';
import { up as assignments } from '../migrations/1700000000006_routing_assignments.js';
import { up as tasks } from '../migrations/1700000000007_task_workflow.js';
import { up as review } from '../migrations/1700000000008_obligation_review.js';
import { up as confidence } from '../migrations/1700000000009_obligation_confidence.js';
import { up as metadata } from '../migrations/1700000000010_document_metadata.js';
import { up as relations } from '../migrations/1700000000011_document_relations.js';
import { createApp } from '../src/app.js';
import type { AuditContext } from '../src/audit.js';
import type { Guard } from '../src/auth/routes.js';
import { PostgresDocumentRepository } from '../src/documents.js';
import type {
  Answer,
  DocumentRelation,
  ExtractionResult,
  IntelligenceClient,
  SearchResponse,
} from '../src/intelligence/client.js';
import { PostgresObligationRepository } from '../src/obligations.js';
import { PostgresRelationRepository, RelationError } from '../src/relations/repository.js';
import { TaskJob } from '../src/tasks/job.js';
import { TaskRepository } from '../src/tasks/repository.js';
import { createTestDatabase, migrationSql } from './postgres.js';

const migrations = [
  initial,
  parents,
  indexing,
  extraction,
  auth,
  audit,
  assignments,
  tasks,
  review,
  confidence,
  metadata,
  relations,
];
const context: AuditContext = { orgId: 42, actorUserId: 11, requestId: 'fixture', ip: null };
const old: Citation = {
  doc_id: 'old',
  page: 1,
  char_start: 0,
  char_end: 16,
  quote: 'File each month.',
  clause_number: '3.2',
};
const other: Citation = {
  ...old,
  char_start: 120,
  char_end: 139,
  quote: 'Retain the receipt.',
  clause_number: '4.1',
};
const current: Citation = { ...old, doc_id: 'newer', quote: 'File each quarter.' };
const proposal: DocumentRelation = {
  id: 'relation-1',
  source_doc_id: 'newer',
  target_doc_id: 'old',
  relation_type: 'supersedes',
  scope: 'whole_document',
  source_citations: [current],
  target_citations: [old],
  confidence: 1,
  status: 'proposed',
  needs_review: false,
  detected_by: 'fixture',
  created_at: '2026-11-20T10:00:00Z',
};
const emptyExtraction: ExtractionResult = {
  doc_id: 'newer',
  obligations: [],
  warnings: [],
  stats: { total: 0, extracted: 0, verified: 0, needs_review: 0, rejected: 0 },
};

async function seed(pool: Pool) {
  await pool.query("INSERT INTO organizations (id, slug) VALUES (42, 'example'), (43, 'other')");
  await pool.query(
    "INSERT INTO users (id, org_id, email, name, password_hash, active) VALUES (11, 42, 'reviewer@example.test', 'Reviewer', 'fixture', true)",
  );
  await pool.query(`
    INSERT INTO documents (id, org_id, external_id, filename, status, doc_date) VALUES
      (1, 42, 'old', 'old.pdf', 'indexed', '2025-01-01'),
      (2, 42, 'newer', 'newer.pdf', 'indexed', '2026-01-01'),
      (3, 42, 'latest', 'latest.pdf', 'indexed', '2027-01-01'),
      (4, 43, 'outside', 'outside.pdf', 'indexed', '2025-01-01')
  `);
  for (const [id, citation, status] of [
    [1, old, 'open'],
    [2, other, 'open'],
    [3, old, 'done'],
  ] as const) {
    await pool.query(
      'INSERT INTO obligations (id,document_id,external_id,action,owner,status,owner_details,deadline,citations,confidence,extractor_version,obligation_type,extracted_status,task_status) ' +
        "VALUES ($1,1,$2,'Original action','reviewer','verified','{}', '{\"resolved\":true,\"normalized_date\":\"2025-01-01\"}', $3::jsonb, 1,'fixture','reporting','verified',$4)",
      [id, 'obl-' + id, JSON.stringify([citation]), status],
    );
    await pool.query(
      "INSERT INTO assignments (obligation_id,assignee_user_id,reason,escalation_due_at) VALUES ($1,11,'rule','2025-02-01')",
      [id],
    );
  }
}

const isManager: Guard =
  (...permissions) =>
  (req, res, next) => {
    if (permissions.includes('relations:review') && req.get('authorization') !== 'Bearer manager') {
      res.sendStatus(403);
      return;
    }
    res.locals.principal = { id: 11, orgId: 42, orgSlug: 'example', roles: [] };
    next();
  };

function clientFor(
  pool: Pool,
  repository: PostgresRelationRepository,
  intelligence: Partial<IntelligenceClient> = {},
) {
  return request(
    createApp(
      {
        intelligence: {
          ingest: vi.fn(),
          index: vi.fn().mockResolvedValue({ doc_id: 'newer' }),
          ...intelligence,
        },
        documents: new PostgresDocumentRepository(pool),
        obligations: new PostgresObligationRepository(pool),
        relations: repository,
        tasks: new TaskRepository(pool, new Map()),
      },
      { guard: isManager },
    ),
  );
}

function manager(call: request.Test) {
  return call.set('Authorization', 'Bearer manager');
}

describe('document relations', () => {
  let database: ReturnType<typeof createTestDatabase>;
  let repository: PostgresRelationRepository;

  beforeEach(async () => {
    database = createTestDatabase();
    for (const migration of migrations) await database.apply(migration);
    await seed(database.pool);
    repository = new PostgresRelationRepository(database.pool);
  }, 15_000);

  afterEach(async () => await database.db.close());

  it('detects after extraction, persists proposals once, and audits detection', async () => {
    const extract = vi
      .fn<IntelligenceClient['extractObligations']>()
      .mockResolvedValue(emptyExtraction);
    const detect = vi.fn<IntelligenceClient['detectRelations']>().mockImplementation(async () => {
      const { rows } = await database.db.query(
        "SELECT extraction_status FROM documents WHERE external_id = 'newer'",
      );
      expect(rows).toEqual([{ extraction_status: 'completed' }]);
      return { doc_id: 'newer', relations: [proposal], warnings: [] };
    });
    const client = clientFor(database.pool, repository, {
      extractObligations: extract,
      detectRelations: detect,
    });
    expect((await client.post('/api/documents/newer/index')).status).toBe(200);
    expect((await client.post('/api/documents/newer/index')).status).toBe(200);
    expect(detect.mock.calls).toEqual([
      [{ doc_id: 'newer', org_id: 'example' }],
      [{ doc_id: 'newer', org_id: 'example' }],
    ]);
    expect((await client.get('/api/relations')).status).toBe(403);
    expect((await manager(client.get('/api/relations'))).body).toMatchObject({
      relations: [{ id: proposal.id }],
    });
    expect(
      (
        await database.db.query(
          "SELECT action FROM audit_log WHERE action LIKE 'relation.%' ORDER BY id",
        )
      ).rows,
    ).toEqual([
      { action: 'relation.propose' },
      { action: 'relation.detect' },
      { action: 'relation.detect' },
    ]);
  });

  it('marks older instructions, pauses open tasks, and audits keep and close', async () => {
    await repository.store({ doc_id: 'newer', relations: [proposal], warnings: [] }, context);
    const client = clientFor(database.pool, repository);
    const result = await manager(client.post('/api/relations/relation-1/confirm'));
    expect(result.status, JSON.stringify(result.body)).toBe(200);
    expect(result.body).toMatchObject({ relation: { status: 'confirmed', reviewed_by: 11 } });
    expect((await manager(client.get('/api/relations/relation-1'))).body).toMatchObject({
      relation: { status: 'confirmed', target_doc_id: 'old' },
    });
    expect((await manager(client.get('/api/relations?status=confirmed'))).body).toMatchObject({
      relations: [{ id: 'relation-1', source_filename: 'newer.pdf', target_filename: 'old.pdf' }],
    });
    expect((await manager(client.get('/api/relations?status=dismissed'))).status).toBe(422);
    expect(await new PostgresDocumentRepository(database.pool).get(42, 'old')).toMatchObject({
      authority_status: 'superseded',
      authority_warnings: [{ relation_id: 'relation-1', newer_doc_id: 'newer' }],
    });
    expect(
      (await database.db.query("SELECT authority_status FROM documents WHERE external_id = 'old'"))
        .rows,
    ).toEqual([{ authority_status: 'superseded' }]);
    expect((await client.get('/api/tasks')).body).toMatchObject({
      tasks: [
        { id: 1, stale: true, stale_relation_ids: ['relation-1'], allowed_transitions: [] },
        { id: 2, stale: true },
        { id: 3, stale: false, status: 'done' },
      ],
    });
    expect(
      (await client.patch('/api/tasks/1/status').send({ status: 'acknowledged' })).status,
    ).toBe(409);
    await new TaskJob(database.pool, new Map([['example', 'manager']])).run(
      new Date('2026-04-01T00:00:00Z'),
    );
    expect(
      (await database.db.query('SELECT id, task_status FROM obligations ORDER BY id')).rows,
    ).toEqual([
      { id: 1, task_status: 'open' },
      { id: 2, task_status: 'open' },
      { id: 3, task_status: 'done' },
    ]);
    expect(
      (
        await database.db.query(
          "SELECT count(*)::int AS count FROM audit_log WHERE action = 'task.escalate'",
        )
      ).rows,
    ).toEqual([{ count: 0 }]);
    const stale = '/api/relations/relation-1/stale-obligations/';
    expect(
      (
        await manager(client.patch(stale + '1')).send({
          decision: 'keep',
          reason: 'Manual exception',
        })
      ).status,
    ).toBe(204);
    expect(
      (await client.patch('/api/tasks/1/status').send({ status: 'acknowledged' })).status,
    ).toBe(200);
    expect(
      (await manager(client.patch(stale + '2')).send({ decision: 'close', reason: 'Replaced' }))
        .status,
    ).toBe(204);
    const { rows } = await database.db.query(
      "SELECT action FROM audit_log WHERE action LIKE 'obligation.stale.%' ORDER BY id",
    );
    expect(rows).toEqual([
      { action: 'obligation.stale.flag' },
      { action: 'obligation.stale.flag' },
      { action: 'obligation.stale.keep' },
      { action: 'obligation.stale.close' },
    ]);
    expect(
      (await database.db.query('SELECT status FROM assignments WHERE obligation_id = 2')).rows,
    ).toEqual([{ status: 'closed' }]);
  });

  it('limits an amendment to affected clauses and retains extracted data after an update', async () => {
    await repository.store(
      { doc_id: 'newer', relations: [{ ...proposal, scope: 'clauses' }], warnings: [] },
      context,
    );
    const client = clientFor(database.pool, repository);
    expect(
      (
        await manager(client.patch('/api/relations/relation-1/type')).send({
          relation_type: 'amends',
        })
      ).status,
    ).toBe(200);
    expect((await manager(client.post('/api/relations/relation-1/confirm'))).status).toBe(200);
    expect(
      (await database.db.query('SELECT authority_status FROM documents WHERE id = 1')).rows,
    ).toEqual([{ authority_status: 'partially_amended' }]);
    expect(
      (await manager(client.get('/api/relations/relation-1/stale-obligations'))).body,
    ).toMatchObject({ obligations: [{ obligation_id: 'obl-1' }] });
    const decision = await manager(
      client.patch('/api/relations/relation-1/stale-obligations/1'),
    ).send({ decision: 'update', action: 'File quarterly', reason: 'Updated filing interval' });
    expect(decision.status).toBe(204);
    expect((await client.get('/api/tasks')).body).toMatchObject({
      tasks: [
        { id: 1, action: 'File quarterly', citations: [current], stale: false },
        { id: 2, action: 'Original action', citations: [other], stale: false },
        { id: 3, stale: false },
      ],
    });
    expect(
      (await database.db.query('SELECT action,citations FROM obligations WHERE id = 1')).rows,
    ).toEqual([{ action: 'Original action', citations: [old] }]);
  });

  it('dismisses with a reason and leaves the older instructions in force', async () => {
    await repository.store({ doc_id: 'newer', relations: [proposal], warnings: [] }, context);
    const client = clientFor(database.pool, repository);
    expect(
      (await manager(client.post('/api/relations/relation-1/dismiss')).send({ reason: '  ' }))
        .status,
    ).toBe(422);
    const result = await manager(client.post('/api/relations/relation-1/dismiss')).send({
      reason: 'Unrelated instructions',
    });
    expect(result.status).toBe(200);
    expect(result.body).toMatchObject({
      relation: { status: 'dismissed', review_reason: 'Unrelated instructions' },
    });
    expect(
      (await database.db.query('SELECT authority_status FROM documents WHERE id = 1')).rows,
    ).toEqual([{ authority_status: 'in_force' }]);
    expect((await database.db.query('SELECT * FROM obligation_staleness')).rows).toEqual([]);
  });

  it('rolls back confirmation, document status and stale flags together on an audit failure', async () => {
    await repository.store({ doc_id: 'newer', relations: [proposal], warnings: [] }, context);
    await database.db.exec(
      "ALTER TABLE audit_log ADD CONSTRAINT test_stale_audit_failure CHECK (action <> 'obligation.stale.flag') NOT VALID",
    );
    await expect(repository.review(proposal.id, { action: 'confirm' }, context)).rejects.toThrow();
    expect(await repository.get(42, proposal.id)).toMatchObject({
      status: 'proposed',
      reviewed_at: null,
    });
    expect(
      (await database.db.query('SELECT authority_status FROM documents WHERE id = 1')).rows,
    ).toEqual([{ authority_status: 'under_review' }]);
    expect((await database.db.query('SELECT * FROM obligation_staleness')).rows).toEqual([]);
  });

  it('warns on stale search results and suppresses stale-cited claims', async () => {
    await repository.store(
      { doc_id: 'newer', relations: [{ ...proposal, relation_type: 'withdraws' }], warnings: [] },
      context,
    );
    await repository.review(proposal.id, { action: 'confirm' }, context);
    const answer: Answer = {
      answer_text: old.quote + '\n' + current.quote,
      claims: [
        { text: old.quote, citations: [old], status: 'verified' },
        { text: current.quote, citations: [current], status: 'verified' },
      ],
      abstained: false,
      abstain_reason: '',
      retrieved_chunk_ids: ['old-chunk'],
      model: 'fixture',
      verifier_version: 'v1',
    };
    const search: SearchResponse = {
      results: [
        {
          chunk: {
            id: 'old-chunk',
            doc_id: old.doc_id,
            page: old.page,
            char_start: old.char_start,
            char_end: old.char_end,
            text: old.quote,
          },
          citations: [old],
          score: 1,
        },
      ],
    };
    const answerMock = vi.fn<IntelligenceClient['answer']>().mockResolvedValue(answer);
    const client = clientFor(database.pool, repository, {
      search: vi.fn().mockResolvedValue(search),
      answer: answerMock,
    });
    const warning = {
      doc_id: 'old',
      newer_doc_id: 'newer',
      status: 'withdrawn',
      relation_id: proposal.id,
    };
    expect((await client.get('/api/search?query=filing&top_k=5')).body).toMatchObject({
      results: [{ stale: true, authority_warnings: [warning] }],
    });
    const query = '/api/answer?query=filing&top_k=5&language=en';
    expect((await client.get(query)).body).toMatchObject({
      stale_sources: true,
      authority_warnings: [warning],
      abstained: false,
      answer_text: current.quote,
      claims: [{ text: current.quote }],
    });
    answerMock.mockResolvedValue({
      ...answer,
      claims: answer.claims.slice(0, 1),
      answer_text: old.quote,
    });
    expect((await client.get(query)).body).toMatchObject({
      stale_sources: true,
      authority_warnings: [warning],
      abstained: true,
      answer_text: '',
      claims: [],
    });
  });
});

it.skipIf(!process.env.RELATION_TEST_DATABASE_URL)(
  'serializes simultaneous confirmations across separate PostgreSQL connections',
  async () => {
    const connectionString = process.env.RELATION_TEST_DATABASE_URL ?? '';
    const schema = 'relation_test_' + randomUUID().replace(/-/g, '');
    const admin = new pg.Pool({ connectionString });
    const pool = new pg.Pool({
      connectionString,
      options: '-c search_path=' + schema,
      statement_timeout: 5000,
    });
    try {
      await admin.query('CREATE SCHEMA ' + schema);
      for (const migration of migrations) await pool.query(migrationSql(migration));
      await seed(pool);
      const service = new PostgresRelationRepository(pool);
      await service.store({ doc_id: 'newer', relations: [proposal], warnings: [] }, context);
      const later: DocumentRelation = {
        ...proposal,
        id: 'relation-2',
        source_doc_id: 'latest',
        source_citations: [{ ...current, doc_id: 'latest' }],
        relation_type: 'withdraws',
      };
      await service.store({ doc_id: 'latest', relations: [later], warnings: [] }, context);
      const results = await Promise.allSettled([
        service.review(proposal.id, { action: 'confirm' }, context),
        service.review(proposal.id, { action: 'confirm' }, context),
        service.review(later.id, { action: 'confirm' }, context),
      ]);
      expect(results.filter((result) => result.status === 'fulfilled')).toHaveLength(2);
      const [failure] = results.filter((result) => result.status === 'rejected');
      expect(failure?.reason).toBeInstanceOf(RelationError);
      expect(failure?.reason).toMatchObject({ status: 409 });
      expect(
        (await pool.query('SELECT authority_status FROM documents WHERE id = 1')).rows,
      ).toEqual([{ authority_status: 'withdrawn' }]);
      expect(
        (
          await pool.query(
            "SELECT count(*)::int AS count FROM audit_log WHERE action = 'relation.confirm'",
          )
        ).rows,
      ).toEqual([{ count: 2 }]);
      expect(
        (await pool.query('SELECT count(*)::int AS count FROM obligation_staleness')).rows,
      ).toEqual([{ count: 4 }]);
    } finally {
      await pool.end();
      await admin.query('DROP SCHEMA IF EXISTS ' + schema + ' CASCADE');
      await admin.end();
    }
  },
  20_000,
);
