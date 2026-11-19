import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';

import { hash } from 'argon2';
import request from 'supertest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { up as initial } from '../migrations/1700000000000_initial.js';
import { up as parents } from '../migrations/1700000000001_document_parents.js';
import { up as indexing } from '../migrations/1700000000002_index_status.js';
import { up as extraction } from '../migrations/1700000000003_obligation_extraction.js';
import { up as users } from '../migrations/1700000000004_auth.js';
import { up as audit } from '../migrations/1700000000005_audit_trail.js';
import { up as assignments } from '../migrations/1700000000006_routing_assignments.js';
import { up as workflow } from '../migrations/1700000000007_task_workflow.js';
import { up as reviewMigration } from '../migrations/1700000000008_obligation_review.js';
import { up as confidenceMigration } from '../migrations/1700000000009_obligation_confidence.js';
import { createApp } from '../src/app.js';
import { inTransaction } from '../src/audit.js';
import { orgPolicy } from '../src/auth/policy.js';
import { PostgresAuthRepository } from '../src/auth/repository.js';
import { AuthService } from '../src/auth/service.js';
import { loadOrgConfig } from '../src/config/loader.js';
import { configuredRouting } from '../src/routing/config.js';
import { PostgresRoutingService } from '../src/routing/service.js';
import { TaskJob } from '../src/tasks/job.js';
import { lockTask, TaskRepository } from '../src/tasks/repository.js';
import { createTestDatabase } from './postgres.js';

const now = new Date('2026-11-19T12:00:00Z');
const password = 'workflow fixture password';
const citation = {
  doc_id: 'doc-one',
  page: 2,
  char_start: 0,
  char_end: 11,
  quote: 'File report',
  clause_number: '4.2',
};

describe('obligation task workflow', () => {
  let database: ReturnType<typeof createTestDatabase>;
  let client: ReturnType<typeof request>;
  let job: TaskJob;
  let routing: PostgresRoutingService;

  beforeEach(async () => {
    database = createTestDatabase();
    const { db, pool, apply } = database;
    for (const migration of [
      initial,
      parents,
      indexing,
      extraction,
      users,
      audit,
      assignments,
      workflow,
      reviewMigration,
      confidenceMigration,
    ]) {
      await apply(migration);
    }
    await db.exec(
      "INSERT INTO organizations (id, slug) VALUES (42, 'example'), (43, 'other');" +
        "INSERT INTO departments (id, org_id, name) VALUES (1, 42, 'finance'), (2, 42, 'legal'), (3, 43, 'finance');" +
        "INSERT INTO roles (id, org_id, name) VALUES (1, 42, 'officer'), (2, 42, 'viewer'), (3, 42, 'manager'), (4, 43, 'officer');" +
        'INSERT INTO documents (id, org_id, external_id, filename, status, doc_type) VALUES ' +
        "(1, 42, 'doc-one', 'agreement.pdf', 'indexed', 'agreement'), " +
        "(2, 42, 'doc-two', 'invoice.pdf', 'indexed', 'invoice'), " +
        "(3, 43, 'doc-other', 'other.pdf', 'indexed', 'invoice');",
    );
    await db.query(
      'INSERT INTO users (id, org_id, department_id, email, name, password_hash, active) VALUES ' +
        "(11, 42, 1, 'owner@example.test', 'Owner', $1, true), " +
        "(12, 42, 1, 'viewer@example.test', 'Viewer', $1, true), " +
        "(13, 42, 2, 'manager@example.test', 'Manager', $1, true), " +
        "(14, 42, 2, 'peer@example.test', 'Peer', $1, true), " +
        "(21, 43, 3, 'owner@example.test', 'Other owner', $1, true)",
      [await hash(password)],
    );
    await db.exec(
      'INSERT INTO user_roles (org_id, user_id, role_id) VALUES (42, 11, 1), (42, 12, 2), ' +
        '(42, 13, 3), (42, 14, 1), (43, 21, 4);',
    );
    await db.query(
      'INSERT INTO obligations (id, document_id, external_id, action, owner, status, task_status, ' +
        'owner_details, deadline, citations, confidence, extractor_version, obligation_type) ' +
        "SELECT id, doc, 'obl-' || id, 'File report ' || id, 'Finance', 'verified', state, " +
        '\'{"resolved": true, "resolved_department": "finance"}\'::jsonb, ' +
        "jsonb_build_object('resolved', resolved, 'normalized_date', due), $1::jsonb, 0.9, 'test', 'reporting' " +
        'FROM (VALUES ' +
        "(1, 1, 'open', '2026-11-18', true), " +
        "(2, 1, 'open', '2026-11-20', true), " +
        "(3, 2, 'open', NULL, false), " +
        "(4, 1, 'open', '2026-11-19', true), " +
        "(5, 2, 'open', '2026-11-22', true), " +
        "(6, 3, 'open', '2026-11-19', true), " +
        "(7, 1, 'in_progress', '2026-11-17', true), " +
        "(8, 1, 'acknowledged', '2026-11-21', true), " +
        "(9, 1, 'done', '2026-11-15', true), " +
        "(10, 2, 'open', '2026-11-15', false), " +
        "(11, 2, 'open', '2026-11-15', true)" +
        ') AS sample(id, doc, state, due, resolved)',
      [JSON.stringify([citation])],
    );
    await db.exec(
      'INSERT INTO assignments (obligation_id, assignee_user_id, queue_type, queue_name, reason, rule_id, escalation_due_at) VALUES ' +
        "(1, 11, NULL, NULL, 'manual', NULL, NULL), " +
        "(2, NULL, 'department', 'finance', 'rule', 'invoice', '2026-11-20T12:00:00Z'), " +
        "(3, NULL, 'role', 'manager', 'review', NULL, NULL), " +
        "(4, NULL, 'role', 'officer', 'rule', 'default', NULL), " +
        "(5, 14, NULL, NULL, 'manual', NULL, NULL), " +
        "(6, 21, NULL, NULL, 'manual', NULL, NULL), " +
        "(7, NULL, 'department', 'finance', 'rule', 'invoice', NULL), " +
        "(8, NULL, 'department', 'legal', 'rule', 'agreement', '2026-11-19T12:00:00Z'), " +
        "(9, NULL, 'department', 'finance', 'rule', 'invoice', '2026-11-18T12:00:00Z'), " +
        "(10, 11, NULL, NULL, 'manual', NULL, NULL);",
    );
    const configPath = new URL('../../../config/orgs/example.yaml', import.meta.url);
    const config = await loadOrgConfig(fileURLToPath(configPath));
    const example = orgPolicy('example', config);
    const other = orgPolicy('other', config);
    const engine = configuredRouting(config);
    if (!example || !other || !engine || !config.routing_policy) throw new Error('Missing policy');
    const review = new Map([['example', config.routing_policy.review_role]]);
    job = new TaskJob(pool, review);
    routing = new PostgresRoutingService(pool, new Map([['example', engine]]), () => now);
    const auth = await AuthService.create(
      new PostgresAuthRepository(pool),
      new Map([
        ['example', example],
        ['other', other],
      ]),
      {
        jwtSecret: randomBytes(32),
        bootstrapHash: 'unused',
        accessSeconds: 60,
        refreshSeconds: 3600,
        rateLimit: 20,
      },
      () => now,
    );
    client = request(
      createApp(
        {
          intelligence: { ingest: vi.fn(), index: vi.fn() },
          documents: { save: vi.fn(), setStatus: vi.fn() },
          tasks: new TaskRepository(pool, review),
        },
        { auth },
      ),
    );
  }, 15_000);

  afterEach(async () => {
    await database.db.close();
  });

  async function login(local: string, org_id = 'example'): Promise<string> {
    const result = await client.post('/api/auth/login').send({
      email: local + '@example.test',
      password,
      org_id,
    });
    expect(result.status).toBe(200);
    return 'Bearer ' + (result.body as { access_token: string }).access_token;
  }

  it('lists personal, department and review queues with source citations and due-date filtering', async () => {
    const owner = await login('owner');
    const manager = await login('manager');
    const other = await login('owner', 'other');
    const feed = await client.get('/api/tasks').set('Authorization', owner);
    expect(feed.status).toBe(200);
    expect(feed.body).toMatchObject({
      tasks: [
        { id: 9, status: 'done' },
        { id: 7, status: 'in_progress' },
        {
          id: 1,
          action: 'File report 1',
          due_date: '2026-11-18',
          citations: [citation],
          scope: 'mine',
        },
        { id: 4, scope: 'mine' },
        { id: 2, scope: 'department' },
        { id: 10, due_date: null },
      ],
    });
    expect(
      (
        await client
          .get(
            '/api/tasks?scope=department&status=open&due_from=2026-11-20&due_to=2026-11-20&doc_id=doc-one',
          )
          .set('Authorization', owner)
      ).body,
    ).toMatchObject({ tasks: [{ id: 2 }] });
    expect(
      (await client.get('/api/tasks?scope=review').set('Authorization', manager)).body,
    ).toMatchObject({ tasks: [{ id: 3 }] });
    expect((await client.get('/api/tasks?scope=review').set('Authorization', owner)).body).toEqual({
      tasks: [],
    });
    expect((await client.get('/api/tasks').set('Authorization', other)).body).toMatchObject({
      tasks: [{ id: 6 }],
    });
    expect(
      (await client.get('/api/tasks?due_from=2026-11-31').set('Authorization', owner)).status,
    ).toBe(422);
  });

  it('checks acting user permissions and audits only allowed transitions with the rejection reason', async () => {
    const owner = await login('owner');
    const viewer = await login('viewer');
    const other = await login('owner', 'other');
    const change = (id: number, token: string, status: string, reason?: string) =>
      client
        .patch('/api/tasks/' + id + '/status')
        .set('Authorization', token)
        .send({ status, reason });
    expect((await client.get('/api/tasks')).status).toBe(401);
    expect((await change(2, viewer, 'acknowledged')).status).toBe(403);
    expect((await change(5, owner, 'acknowledged')).status).toBe(404);
    expect((await change(1, other, 'acknowledged')).status).toBe(404);
    expect((await change(1, owner, 'done')).status).toBe(409);
    expect((await change(1, owner, 'overdue')).status).toBe(409);
    expect((await change(2, owner, 'rejected', '  ')).status).toBe(422);
    for (const status of ['acknowledged', 'in_progress', 'done']) {
      const accepted = await change(1, owner, status);
      expect(accepted.status).toBe(200);
      expect(accepted.body).toMatchObject({ task: { status } });
    }
    const rejected = await change(2, owner, 'rejected', '  Superseded  ');
    expect(rejected.body).toMatchObject({
      task: { status: 'rejected', rejection_reason: 'Superseded' },
    });
    expect((await change(2, owner, 'acknowledged')).status).toBe(409);
    const { rows } = await database.db.query(
      'SELECT entity_id, actor_user_id, "before", "after" FROM audit_log WHERE action = \'task.transition\' ORDER BY id',
    );
    expect(rows).toMatchObject([
      {
        entity_id: '1',
        actor_user_id: 11,
        before: { status: 'open' },
        after: { status: 'acknowledged' },
      },
      {
        entity_id: '1',
        actor_user_id: 11,
        before: { status: 'acknowledged' },
        after: { status: 'in_progress' },
      },
      {
        entity_id: '1',
        actor_user_id: 11,
        before: { status: 'in_progress' },
        after: { status: 'done' },
      },
      {
        entity_id: '2',
        actor_user_id: 11,
        before: { status: 'open' },
        after: { status: 'rejected', rejection_reason: 'Superseded' },
      },
    ]);
  });

  it('marks only past-due open tasks and escalates once without changing workflow progress', async () => {
    await job.run(now);
    await job.run(now);
    const { db, pool } = database;
    expect((await db.query('SELECT id, task_status FROM obligations ORDER BY id')).rows).toEqual([
      { id: 1, task_status: 'overdue' },
      { id: 2, task_status: 'open' },
      { id: 3, task_status: 'open' },
      { id: 4, task_status: 'open' },
      { id: 5, task_status: 'open' },
      { id: 6, task_status: 'open' },
      { id: 7, task_status: 'in_progress' },
      { id: 8, task_status: 'acknowledged' },
      { id: 9, task_status: 'done' },
      { id: 10, task_status: 'open' },
      { id: 11, task_status: 'open' },
    ]);
    expect(
      (
        await db.query(
          'SELECT status, queue_name, reason, rule_id FROM assignments WHERE obligation_id = 8 ORDER BY id',
        )
      ).rows,
    ).toEqual([
      { status: 'closed', queue_name: 'legal', reason: 'rule', rule_id: 'agreement' },
      { status: 'active', queue_name: 'manager', reason: 'escalation', rule_id: 'agreement' },
    ]);
    expect(
      (await db.query('SELECT queue_name, status FROM assignments WHERE obligation_id = 2')).rows,
    ).toEqual([{ queue_name: 'finance', status: 'active' }]);
    await expect(
      inTransaction(pool, async (connection) => {
        await lockTask(connection, 8, 42);
        return routing.assign(
          connection,
          {
            rowId: 8,
            docId: 'doc-one',
            externalId: 'obl-8',
            orgSlug: 'example',
            obligationType: 'reporting',
            documentType: 'agreement',
            ownerDepartment: 'finance',
            ownerResolved: true,
            action: 'File report 8',
            condition: null,
            deadline: '2026-11-21',
            confidence: 0.9,
            status: 'verified',
          },
          { orgId: 42, actorUserId: null, requestId: 'repeat-extraction', ip: null },
          true,
        );
      }),
    ).resolves.toMatchObject({ reason: 'escalation' });
    const events = await db.query(
      'SELECT action, entity_id, actor_user_id, "before", "after" FROM audit_log WHERE entity_type = \'task\' ORDER BY id',
    );
    expect(events.rows).toMatchObject([
      {
        action: 'task.transition',
        entity_id: '1',
        actor_user_id: null,
        before: { status: 'open' },
        after: { status: 'overdue' },
      },
      {
        action: 'task.escalate',
        entity_id: '8',
        actor_user_id: null,
        before: { queue_name: 'legal' },
        after: { queue_name: 'manager' },
      },
    ]);
  });
});
