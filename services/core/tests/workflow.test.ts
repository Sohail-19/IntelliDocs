import { createHash, randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';

import request from 'supertest';
import { expect, it, vi } from 'vitest';

import { up as initial } from '../migrations/1700000000000_initial.js';
import { up as parents } from '../migrations/1700000000001_document_parents.js';
import { up as indexing } from '../migrations/1700000000002_index_status.js';
import { up as extraction } from '../migrations/1700000000003_obligation_extraction.js';
import { up as accounts } from '../migrations/1700000000004_auth.js';
import { up as audit } from '../migrations/1700000000005_audit_trail.js';
import { up as assignments } from '../migrations/1700000000006_routing_assignments.js';
import { up as tasks } from '../migrations/1700000000007_task_workflow.js';
import { up as review } from '../migrations/1700000000008_obligation_review.js';
import { up as confidenceMigration } from '../migrations/1700000000009_obligation_confidence.js';
import { createApp } from '../src/app.js';
import { PostgresAuditReader } from '../src/audit.js';
import type { AuditEntry } from '../src/audit.js';
import { orgPolicy } from '../src/auth/policy.js';
import { PostgresAuthRepository } from '../src/auth/repository.js';
import { AuthService } from '../src/auth/service.js';
import { loadOrgConfig } from '../src/config/loader.js';
import { PostgresDocumentRepository } from '../src/documents.js';
import type { IngestResponse, IntelligenceClient } from '../src/intelligence/client.js';
import { PostgresObligationRepository } from '../src/obligations.js';
import type { DocumentExtractionResult, ExtractedObligation } from '../src/obligations.js';
import { ObligationReviewRepository } from '../src/review/repository.js';
import { configuredRouting } from '../src/routing/config.js';
import { PostgresRoutingService } from '../src/routing/service.js';
import { TaskRepository } from '../src/tasks/repository.js';
import { createTestDatabase } from './postgres.js';

const docId = 'invoice-doc';
const filename = 'invoice.pdf';
const now = new Date('2026-11-19T12:00:00Z');
const reportQuote = 'Finance must file the report by 28 November 2026.';
const paymentQuote = 'The office must make the payment by 28 November 2026.';
const citation = {
  doc_id: docId,
  page: 2,
  char_start: 42,
  char_end: 42 + reportQuote.length,
  quote: reportQuote,
  clause_number: '4.2',
};
const noEvidence = {
  quote_exact: 0,
  quote_fuzzy: 0,
  owner_resolved: 0,
  deadline_resolved: 0,
  recognized_cue: 0,
  cross_chunk_agreement: 0,
};
const verifiedEvidence = {
  ...noEvidence,
  quote_exact: 0.25,
  owner_resolved: 0.55,
  deadline_resolved: 0.1,
  recognized_cue: 0.05,
};
const unresolvedEvidence = { ...verifiedEvidence, owner_resolved: 0 };
const verified: ExtractedObligation = {
  id: 'auto',
  doc_id: docId,
  action: 'File the report',
  owner: {
    raw_text: 'Finance',
    resolved_department: 'finance',
    resolved_role: null,
    resolved: true,
  },
  deadline: {
    raw_text: '28 November',
    normalized_date: '2026-11-28',
    resolved: true,
    anchor_date: null,
    is_relative: false,
  },
  obligation_type: 'reporting',
  citations: [citation],
  confidence: 0.95,
  confidence_breakdown: verifiedEvidence,
  status: 'verified',
  extractor_version: 'fixture',
};
const results: DocumentExtractionResult = {
  doc_id: docId,
  obligations: [
    verified,
    {
      ...verified,
      id: 'pending',
      action: 'Make the payment',
      owner: {
        raw_text: 'The office',
        resolved_department: null,
        resolved_role: null,
        resolved: false,
      },
      obligation_type: 'payment',
      citations: [{ ...citation, char_end: 42 + paymentQuote.length, quote: paymentQuote }],
      confidence: 0.4,
      confidence_breakdown: unresolvedEvidence,
      status: 'needs_review',
    },
    {
      ...verified,
      id: 'unsupported',
      citations: [],
      confidence: 0,
      confidence_breakdown: noEvidence,
      status: 'rejected',
    },
    { ...verified, id: 'low-confidence', confidence: 0, confidence_breakdown: noEvidence },
  ],
  warnings: [],
  stats: { total: 4, extracted: 0, verified: 2, rejected: 1, needs_review: 1 },
};
const parsed: IngestResponse = {
  document_id: docId,
  parse_result: {
    document: {
      id: docId,
      filename,
      mime_type: 'application/pdf',
      source_uri: 'file:///invoice.pdf',
      status: 'ready',
      doc_type: 'invoice',
      pages: [],
    },
    parser_name: 'test',
    parser_version: '1',
    warnings: [],
  },
};

async function setup(database: ReturnType<typeof createTestDatabase>) {
  const { apply, pool } = database;
  for (const migration of [
    initial,
    parents,
    indexing,
    extraction,
    accounts,
    audit,
    assignments,
    tasks,
    review,
    confidenceMigration,
  ]) {
    await apply(migration);
  }
  const config = await loadOrgConfig(
    fileURLToPath(new URL('../../../config/orgs/example.yaml', import.meta.url)),
  );
  const reportsRule = {
    id: 'reports-owner',
    priority: 50,
    conditions: { obligation_type: 'reporting', owner_department: 'finance' },
    target: { kind: 'user', value: 'owner@example.test' },
  } as const;
  const policy = orgPolicy('example', config);
  const engine = configuredRouting({
    ...config,
    routing_rules: [reportsRule, ...config.routing_rules],
  });
  if (!engine || !policy || !config.routing_policy) throw new Error('Missing test policy');
  const routing = new PostgresRoutingService(pool, new Map([['example', engine]]), () => now);
  const role = new Map([['example', config.routing_policy.review_role]]);
  const users = new PostgresAuthRepository(pool);
  await users.seed('example', policy.departments, [...policy.roles.keys()]);
  const bootstrap = randomBytes(32).toString('base64url');
  const auth = await AuthService.create(
    users,
    new Map([['example', policy]]),
    {
      jwtSecret: randomBytes(32),
      bootstrapHash: createHash('sha256').update(bootstrap).digest('hex'),
      accessSeconds: 60,
      refreshSeconds: 3600,
      rateLimit: 20,
    },
    () => now,
  );
  const intelligence = {
    ingest: vi.fn<IntelligenceClient['ingest']>().mockResolvedValue(parsed),
    index: vi
      .fn<IntelligenceClient['index']>()
      .mockResolvedValue({ doc_id: docId, chunk_count: 1, embedding_model: 'fake', warnings: [] }),
    extractObligations: vi
      .fn<IntelligenceClient['extractObligations']>()
      .mockResolvedValue(results),
  };
  const client = request(
    createApp(
      {
        intelligence,
        documents: new PostgresDocumentRepository(pool),
        obligations: new PostgresObligationRepository(pool, routing),
        routing,
        tasks: new TaskRepository(pool, role),
        reviews: new ObligationReviewRepository(pool, routing, new Map([['example', config]])),
        audit: new PostgresAuditReader(pool),
      },
      { auth },
    ),
  );
  const password = 'end-to-end fixture password';

  async function login(email: string) {
    const response = await client
      .post('/api/auth/login')
      .send({ email, password, org_id: 'example' });
    expect(response.status).toBe(200);
    return 'Bearer ' + (response.body as { access_token: string }).access_token;
  }

  const adminEmail = 'admin@example.test';
  const registered = await client.post('/api/auth/register').send({
    email: adminEmail,
    password,
    name: 'Admin',
    org_id: 'example',
    bootstrap_token: bootstrap,
  });
  expect(registered.status).toBe(201);
  const admin = await login(adminEmail);
  async function member(name: string, memberRole: string) {
    const email = name + '@example.test';
    const response = await client
      .post('/api/auth/register')
      .set('Authorization', admin)
      .send({
        email,
        password,
        name,
        org_id: 'example',
        department: 'finance',
        roles: [memberRole],
      });
    expect(response.status).toBe(201);
    return login(email);
  }
  const owner = await member('owner', 'finance_officer');
  const manager = await member('manager', 'manager');
  return { client, intelligence, admin, owner, manager };
}

it('uploads, indexes, extracts, routes, and completes an assigned task with a linked audit trail', async () => {
  const database = createTestDatabase();
  try {
    const { client, intelligence, admin, owner } = await setup(database);
    const upload = await client
      .post('/v1/upload')
      .set('Authorization', admin)
      .field('doc_type', 'invoice')
      .attach('file', Buffer.from('%PDF synthetic fixture'), {
        filename,
        contentType: 'application/pdf',
      });
    expect(upload.status).toBe(202);
    expect(intelligence.ingest).toHaveBeenCalledWith(
      expect.objectContaining({ org_id: 'example', doc_type: 'invoice', index: false }),
      filename,
    );
    expect(intelligence.index.mock.calls).toEqual([[{ doc_id: docId }]]);
    expect(intelligence.extractObligations.mock.calls).toEqual([[{ doc_id: docId }]]);
    expect(intelligence.ingest.mock.invocationCallOrder.at(0)).toBeLessThan(
      intelligence.index.mock.invocationCallOrder[0] ?? 0,
    );
    expect(intelligence.index.mock.invocationCallOrder.at(0)).toBeLessThan(
      intelligence.extractObligations.mock.invocationCallOrder[0] ?? 0,
    );

    const feed = await client.get('/api/tasks?scope=mine').set('Authorization', owner);
    expect(feed.status).toBe(200);
    expect(feed.body).toMatchObject({
      tasks: [
        { obligation_id: 'auto', status: 'open', due_date: '2026-11-28', citations: [citation] },
      ],
    });
    const [task] = (feed.body as { tasks: { id: number; assignee_user_id: number }[] }).tasks;
    if (!task) throw new Error('Missing assigned task');
    const scored = await client
      .get('/api/obligations')
      .query({ doc_id: docId })
      .set('Authorization', owner);
    expect(scored.body).toMatchObject({
      obligations: [
        {
          id: 'auto',
          status: 'verified',
          confidence: 0.95,
          confidence_breakdown: verifiedEvidence,
        },
        { id: 'low-confidence', status: 'needs_review' },
        {
          id: 'pending',
          status: 'needs_review',
          confidence: 0.4,
          confidence_breakdown: unresolvedEvidence,
        },
        { id: 'unsupported', status: 'needs_review' },
      ],
    });
    const statuses = ['acknowledged', 'in_progress', 'done'] as const;
    const requestIds = [];
    for (const status of statuses) {
      const changed = await client
        .patch('/api/tasks/' + task.id + '/status')
        .set('Authorization', owner)
        .send({ status });
      expect(changed.status).toBe(200);
      expect(changed.body).toMatchObject({ task: { status } });
      requestIds.push(changed.get('X-Request-ID'));
    }
    const log = await client.get('/api/audit?limit=100').set('Authorization', admin);
    expect(log.status).toBe(200);
    const { entries } = log.body as { entries: AuditEntry[] };
    const uploadTrail = entries
      .filter(({ request_id }) => request_id === upload.get('X-Request-ID'))
      .reverse();
    expect(uploadTrail).toMatchObject([
      { action: 'document.upload', entity_id: docId, after: { status: 'parsed' } },
      {
        action: 'document.index',
        entity_id: docId,
        before: { status: 'parsed' },
        after: { status: 'indexed' },
      },
      {
        action: 'obligation.route',
        entity_id: 'auto',
        before: null,
        after: { assignee_user_id: task.assignee_user_id, rule_id: 'reports-owner' },
      },
      {
        action: 'obligation.route',
        entity_id: 'pending',
        after: { reason: 'review', queue_name: 'manager' },
      },
      { action: 'obligation.route', entity_id: 'unsupported', after: { reason: 'review' } },
      { action: 'obligation.route', entity_id: 'low-confidence', after: { reason: 'review' } },
      {
        action: 'obligations.extraction',
        entity_id: docId,
        before: { extraction_status: 'pending' },
        after: { extraction_status: 'completed' },
      },
    ]);
    for (const [position, previous] of ['open', 'acknowledged', 'in_progress'].entries()) {
      expect(entries.find(({ request_id }) => request_id === requestIds[position])).toMatchObject({
        action: 'task.transition',
        entity_id: String(task.id),
        actor_user_id: task.assignee_user_id,
        before: { status: previous },
        after: { status: statuses[position] },
      });
    }
  } finally {
    await database.db.close();
  }
}, 15_000);

it('reviews pending obligations, preserves extracted values, and routes approval and rejection atomically', async () => {
  const database = createTestDatabase();
  try {
    const { client, admin, owner, manager } = await setup(database);
    expect(
      (
        await client
          .post('/v1/upload')
          .set('Authorization', admin)
          .attach('file', Buffer.from('%PDF'), filename)
      ).status,
    ).toBe(202);
    const list = '/api/obligations/review?doc_id=' + docId;
    const resource = '/api/documents/' + docId + '/obligations/';
    const pending = resource + 'pending/review';
    const unsupported = resource + 'unsupported/review';
    expect((await client.get(list).set('Authorization', owner)).status).toBe(403);
    const queued = await client.get(list).set('Authorization', manager);
    expect(queued.body).toMatchObject({
      obligations: [
        { id: 'low-confidence', status: 'needs_review', extracted_status: 'verified' },
        {
          id: 'pending',
          status: 'needs_review',
          extracted_status: 'needs_review',
          confidence: 0.4,
          confidence_breakdown: unresolvedEvidence,
          assignment: { reason: 'review', queue_name: 'manager' },
        },
        { id: 'unsupported', status: 'needs_review', extracted_status: 'rejected' },
      ],
    });
    expect((await client.post(pending + '/approve').set('Authorization', owner)).status).toBe(403);
    expect((await client.post(pending + '/approve').set('Authorization', manager)).status).toBe(
      409,
    );
    expect(
      (
        await client
          .patch(resource + 'pending/status')
          .set('Authorization', manager)
          .send({ status: 'verified' })
      ).status,
    ).toBe(409);
    expect(
      (
        await client
          .patch(pending)
          .set('Authorization', manager)
          .send({ owner: { department: 'legal', role: 'finance_officer' } })
      ).status,
    ).toBe(422);
    expect(
      (await client.patch(pending).set('Authorization', manager).send({ due_date: '2026-02-30' }))
        .status,
    ).toBe(422);

    const patched = await client
      .patch(pending)
      .set('Authorization', manager)
      .send({ owner: { role: 'finance_officer' }, due_date: '2026-11-20' });
    expect(patched.status).toBe(200);
    expect(patched.body).toMatchObject({
      obligation: {
        status: 'needs_review',
        owner_override: { resolved_department: 'finance', resolved_role: 'finance_officer' },
        extracted_owner: { raw_text: 'The office', resolved: false },
        deadline_override: { normalized_date: '2026-11-20' },
        extracted_deadline: { normalized_date: '2026-11-28' },
      },
    });
    const approved = await client.post(pending + '/approve').set('Authorization', manager);
    expect(approved.status).toBe(200);
    expect(approved.body).toMatchObject({
      obligation: {
        status: 'verified',
        review_decision: 'approved',
        assignment: {
          queue_type: 'role',
          queue_name: 'finance_officer',
          rule_id: 'urgent-payment',
        },
      },
    });
    expect((await client.post(pending + '/approve').set('Authorization', manager)).status).toBe(
      409,
    );
    const feed = await client
      .get('/api/tasks?status=open&due_to=2026-11-20')
      .set('Authorization', owner);
    expect(feed.body).toMatchObject({
      tasks: [{ obligation_id: 'pending', due_date: '2026-11-20' }],
    });

    expect((await client.post(unsupported + '/approve').set('Authorization', manager)).status).toBe(
      409,
    );
    expect(
      (
        await client
          .post(unsupported + '/reject')
          .set('Authorization', manager)
          .send({ reason: '  ' })
      ).status,
    ).toBe(422);
    const rejected = await client
      .post(unsupported + '/reject')
      .set('Authorization', manager)
      .send({ reason: 'Source span could not be verified' });
    expect(rejected.status).toBe(200);
    expect(rejected.body).toMatchObject({
      obligation: { status: 'rejected', review_decision: 'rejected', assignment: null },
    });
    expect(
      (await client.post(resource + 'unsupported/route').set('Authorization', manager)).status,
    ).toBe(409);

    expect(
      (await client.post('/api/documents/' + docId + '/obligations').set('Authorization', manager))
        .status,
    ).toBe(200);
    expect((await client.get(list).set('Authorization', manager)).body).toMatchObject({
      obligations: [{ id: 'low-confidence' }],
    });
    const refreshedFeed = await client
      .get('/api/tasks?status=open&due_to=2026-11-20')
      .set('Authorization', owner);
    expect(refreshedFeed.body).toEqual(feed.body);
    const changes = await database.db.query(
      'SELECT action, entity_id, actor_user_id, "before", "after" FROM audit_log WHERE action LIKE $1 ORDER BY id',
      ['obligation.review.%'],
    );
    expect(changes.rows).toMatchObject([
      {
        action: 'obligation.review.edit',
        entity_id: 'pending',
        before: { owner: { resolved: false } },
        after: { owner: { resolved: true } },
      },
      {
        action: 'obligation.review.approve',
        entity_id: 'pending',
        before: { status: 'needs_review' },
        after: { status: 'verified' },
      },
      {
        action: 'obligation.review.reject',
        entity_id: 'unsupported',
        before: { assignment: { queue_name: 'manager' } },
        after: { assignment: null, review_reason: 'Source span could not be verified' },
      },
    ]);
  } finally {
    await database.db.close();
  }
}, 15_000);
