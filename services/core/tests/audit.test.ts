import { createHash, randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

import request from 'supertest';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

import { up as initial } from '../migrations/1700000000000_initial.js';
import { up as parents } from '../migrations/1700000000001_document_parents.js';
import { up as indexing } from '../migrations/1700000000002_index_status.js';
import { up as extraction } from '../migrations/1700000000003_obligation_extraction.js';
import { up as authMigration } from '../migrations/1700000000004_auth.js';
import { up as auditMigration } from '../migrations/1700000000005_audit_trail.js';
import { up as assignmentMigration } from '../migrations/1700000000006_routing_assignments.js';
import { up as workflowMigration } from '../migrations/1700000000007_task_workflow.js';
import { up as reviewMigration } from '../migrations/1700000000008_obligation_review.js';
import { up as confidenceMigration } from '../migrations/1700000000009_obligation_confidence.js';
import { up as documentMetadata } from '../migrations/1700000000010_document_metadata.js';
import { up as documentRelations } from '../migrations/1700000000011_document_relations.js';
import { createApp } from '../src/app.js';
import { PostgresAuditReader } from '../src/audit.js';
import type { AuditEntry } from '../src/audit.js';
import { orgPolicy } from '../src/auth/policy.js';
import { PostgresAuthRepository } from '../src/auth/repository.js';
import { AuthService } from '../src/auth/service.js';
import { loadOrgConfig } from '../src/config/loader.js';
import { PostgresDocumentRepository } from '../src/documents.js';
import type { ExtractionResult, IngestResponse } from '../src/intelligence/client.js';
import { PostgresObligationRepository } from '../src/obligations.js';

import { createTestDatabase } from './postgres.js';

const { db, pool, apply } = createTestDatabase();
const bootstrap = randomBytes(32).toString('base64url');
const password = 'fixture passphrase to hash';
const adminEmail = 'admin@example.test';
let client: ReturnType<typeof request>;

const uploaded: IngestResponse = {
  document_id: 'doc-001',
  parse_result: {
    document: {
      id: 'doc-001',
      filename: 'sample.pdf',
      mime_type: 'application/pdf',
      source_uri: 'file:///sample.pdf',
      status: 'ready',
      pages: [],
    },
    parser_name: 'test',
    parser_version: '1',
    warnings: [],
  },
};

beforeAll(async () => {
  for (const migrate of [initial, parents, indexing, extraction, authMigration])
    await apply(migrate);
  await db.exec(
    "INSERT INTO organizations (slug) VALUES ('example'); " +
      'INSERT INTO documents (external_id, filename, status, org_id) ' +
      "SELECT 'legacy', 'legacy.pdf', 'indexed', id FROM organizations WHERE slug = 'example'; " +
      'INSERT INTO audit_log (event_type, payload, created_at) ' +
      "VALUES ('obligations.extraction', '{\"doc_id\":\"legacy\"}', '2020-01-02T23:59:59Z');",
  );
  await apply(auditMigration);
  await apply(assignmentMigration);
  await apply(workflowMigration);
  await apply(reviewMigration);
  await apply(confidenceMigration);
  await apply(documentMetadata);
  await apply(documentRelations);
  const config = await loadOrgConfig(
    fileURLToPath(new URL('../../../config/orgs/example.yaml', import.meta.url)),
  );
  const policy = orgPolicy('example', config);
  if (!policy) throw new Error('Example policy is missing');
  const users = new PostgresAuthRepository(pool);
  await users.seed('example', policy.departments, [...policy.roles.keys()]);
  await users.seed('another', policy.departments, [...policy.roles.keys()]);
  await db.exec(
    'INSERT INTO audit_log (org_id, action, entity_type, entity_id, request_id) ' +
      "SELECT id, 'other.event', 'document', 'doc-001', 'other-request' FROM organizations WHERE slug = 'another'",
  );
  const auth = await AuthService.create(users, new Map([['example', policy]]), {
    jwtSecret: randomBytes(32),
    bootstrapHash: createHash('sha256').update(bootstrap).digest('hex'),
    rateLimit: 30,
    accessSeconds: 900,
    refreshSeconds: 3600,
  });
  const result = JSON.parse(
    await readFile(
      new URL('../../../contracts/examples/extraction-result.v1.json', import.meta.url),
      'utf8',
    ),
  ) as ExtractionResult;
  client = request(
    createApp(
      {
        audit: new PostgresAuditReader(pool),
        documents: new PostgresDocumentRepository(pool),
        obligations: new PostgresObligationRepository(pool),
        intelligence: {
          ingest: vi.fn().mockResolvedValue(uploaded),
          index: vi.fn().mockResolvedValue({
            doc_id: 'doc-001',
            chunk_count: 1,
            embedding_model: 'fixture',
            warnings: [],
          }),
          extractObligations: vi.fn().mockResolvedValue(result),
        },
      },
      { auth },
    ),
  );
}, 15_000);

afterAll(async () => {
  await db.close();
});

function register(email: string, fields: Record<string, unknown>, bearer?: string) {
  const call = client.post('/api/auth/register');
  if (bearer) call.set('Authorization', bearer);
  return call.send({ name: 'Sample user', org_id: 'example', email, password, ...fields });
}

async function rowsFor(response: { headers: Record<string, unknown> }): Promise<AuditEntry[]> {
  const requestId = response.headers['x-request-id'];
  expect(requestId).toMatch(/^[a-f0-9-]{36}$/);
  return (
    await db.query<AuditEntry>('SELECT * FROM audit_log WHERE request_id = $1 ORDER BY id', [
      requestId,
    ])
  ).rows;
}

async function login(email: string) {
  const response = await client
    .post('/api/auth/login')
    .send({ org_id: 'example', email, password });
  expect(response.status).toBe(200);
  const tokens = response.body as { access_token: string; refresh_token: string };
  const events = await rowsFor(response);
  expect(events).toHaveLength(1);
  const [event] = events;
  expect(event).toMatchObject({
    action: 'auth.login',
    before: null,
    after: { session_created: true },
  });
  const recorded = JSON.stringify(event);
  expect(recorded).not.toContain(password);
  expect(recorded).not.toContain(tokens.access_token);
  expect(recorded).not.toContain(tokens.refresh_token);
  return {
    bearer: 'Bearer ' + tokens.access_token,
    refresh: tokens.refresh_token,
    id: event?.actor_user_id,
  };
}

describe('core audit trail with PostgreSQL', () => {
  it('audits each state change once and limits the audit API to authorized org roles', async () => {
    const registered = await register(adminEmail, { bootstrap_token: bootstrap });
    expect(registered.status).toBe(201);
    expect(await rowsFor(registered)).toMatchObject([
      { action: 'user.register', actor_user_id: null, before: null, after: { roles: ['admin'] } },
    ]);
    const admin = await login(adminEmail);
    const managerEmail = 'manager@example.test';
    const viewerEmail = 'viewer@example.test';
    expect((await register(managerEmail, { roles: ['manager'] }, admin.bearer)).status).toBe(201);
    expect((await register(viewerEmail, {}, admin.bearer)).status).toBe(201);
    const manager = await login(managerEmail);
    const viewer = await login(viewerEmail);
    expect(viewer.id).toEqual(expect.any(Number));
    expect((await client.get('/api/audit')).status).toBe(401);
    expect((await client.get('/api/audit').set('Authorization', viewer.bearer)).status).toBe(403);

    const upload = await client
      .post('/v1/upload')
      .set('Authorization', admin.bearer)
      .set('X-Request-ID', 'client-controlled')
      .attach('file', Buffer.from('test'), 'sample.pdf');
    expect(upload.status).toBe(202);
    expect(await rowsFor(upload)).toMatchObject([
      { action: 'document.upload', before: null, after: { status: 'parsed' } },
      { action: 'document.index', before: { status: 'parsed' }, after: { status: 'indexed' } },
      {
        action: 'obligations.extraction',
        before: { obligations: [] },
        after: { obligations: [{ id: 'obl-001', status: 'needs_review' }] },
      },
    ]);
    const obligation = '/api/documents/doc-001/obligations/obl-001';
    const status = await client
      .patch(obligation + '/status')
      .set('Authorization', manager.bearer)
      .send({ status: 'verified' });
    expect(status.status).toBe(204);
    const statusEvents = await rowsFor(status);
    expect(statusEvents[0]?.ip).toMatch(/^[.:\da-f]+$/i);
    expect(statusEvents).toMatchObject([
      {
        action: 'obligation.status',
        actor_user_id: manager.id,
        before: { status: 'needs_review' },
        after: { status: 'verified' },
      },
    ]);
    const assignment = await client
      .patch(obligation + '/assignment')
      .set('Authorization', manager.bearer)
      .send({ assigned_user_id: viewer.id });
    expect(assignment.status).toBe(204);
    expect(await rowsFor(assignment)).toMatchObject([
      {
        action: 'obligation.assignment',
        before: { assigned_user_id: null },
        after: { assigned_user_id: viewer.id },
      },
    ]);
    const reindex = await client
      .post('/api/documents/doc-001/index')
      .set('Authorization', admin.bearer);
    expect(reindex.status).toBe(200);
    expect(await rowsFor(reindex)).toMatchObject([
      { action: 'document.index', before: { status: 'indexed' }, after: { status: 'indexed' } },
      {
        action: 'obligations.extraction',
        before: { obligations: [{ status: 'verified' }] },
        after: { obligations: [{ assigned_user_id: viewer.id }] },
      },
    ]);
    const roles = await client
      .put('/api/users/' + viewer.id + '/roles')
      .set('Authorization', admin.bearer)
      .send({ roles: ['officer'] });
    expect(roles.status).toBe(204);
    expect(await rowsFor(roles)).toMatchObject([
      { action: 'user.roles', before: { roles: ['viewer'] }, after: { roles: ['officer'] } },
    ]);
    const refresh = await client.post('/api/auth/refresh').send({ refresh_token: admin.refresh });
    expect(refresh.status).toBe(200);
    const refreshed = await rowsFor(refresh);
    expect(refreshed).toMatchObject([
      {
        action: 'auth.refresh',
        before: { expires_at: expect.any(String) as unknown },
        after: { expires_at: expect.any(String) as unknown },
      },
    ]);
    expect(JSON.stringify(refreshed)).not.toContain(admin.refresh);
    const { refresh_token: fresh } = refresh.body as { refresh_token: string };
    const logout = await client.post('/api/auth/logout').send({ refresh_token: fresh });
    expect(logout.status).toBe(204);
    const logoutAudit = await rowsFor(logout);
    expect(logoutAudit).toMatchObject([
      { action: 'auth.logout', actor_user_id: admin.id, after: { session_active: false } },
    ]);
    expect(JSON.stringify(logoutAudit)).not.toContain(fresh);
    expect((await client.post('/api/auth/refresh').send({ refresh_token: fresh })).status).toBe(
      401,
    );
    const repeatedLogout = await client.post('/api/auth/logout').send({ refresh_token: fresh });
    expect(repeatedLogout.status).toBe(204);
    expect(await rowsFor(repeatedLogout)).toEqual([]);

    const query = {
      entity_type: 'document',
      entity_id: 'doc-001',
      actor_user_id: admin.id,
      limit: 2,
    };
    const first = await client.get('/api/audit').set('Authorization', manager.bearer).query(query);
    expect(first.status).toBe(200);
    const page = first.body as { entries: AuditEntry[]; next_cursor: number | null };
    expect(page.entries.map((entry) => entry.action)).toEqual([
      'obligations.extraction',
      'document.index',
    ]);
    expect(page.next_cursor).toBe(page.entries[1]?.id);
    const second = await client
      .get('/api/audit')
      .set('Authorization', manager.bearer)
      .query({ ...query, before_id: page.next_cursor });
    const next = second.body as typeof page;
    expect(next.entries.map((entry) => entry.action)).toEqual([
      'obligations.extraction',
      'document.index',
    ]);
    expect(next.entries[0]?.id).toBeLessThan(page.next_cursor ?? 0);
    const indexed = await client
      .get('/api/audit')
      .set('Authorization', manager.bearer)
      .query({ ...query, action: 'document.index' });
    const indexPage = indexed.body as typeof page;
    expect(indexPage.entries.map((entry) => entry.action)).toEqual([
      'document.index',
      'document.index',
    ]);
    expect(indexPage.next_cursor).toBeNull();
    const dated = await client
      .get('/api/audit')
      .set('Authorization', admin.bearer)
      .query({ from: '2020-01-02', to: '2020-01-02', actor_user_id: 'system' });
    expect(dated.body).toMatchObject({
      entries: [{ entity_id: 'legacy', request_id: 'legacy-1' }],
      next_cursor: null,
    });
    const all = await client.get('/api/audit').set('Authorization', manager.bearer);
    expect(JSON.stringify(all.body)).not.toContain('other.event');
    expect(
      (await client.get('/api/audit?limit=101').set('Authorization', admin.bearer)).status,
    ).toBe(422);

    await db.exec(
      "ALTER TABLE audit_log ADD CONSTRAINT test_assignment_failure CHECK (action <> 'obligation.assignment') NOT VALID",
    );
    const failed = await client
      .patch(obligation + '/assignment')
      .set('Authorization', manager.bearer)
      .send({ assigned_user_id: null });
    expect(failed.status).toBe(500);
    expect(await rowsFor(failed)).toEqual([]);
    const read = await client
      .get('/api/documents/doc-001/obligations')
      .set('Authorization', manager.bearer);
    expect(read.body).toMatchObject({ obligations: [{ assigned_user_id: viewer.id }] });
  }, 15_000);

  it('blocks update, delete and truncate at the database', async () => {
    for (const sql of [
      "UPDATE audit_log SET action = 'changed' WHERE id = 1",
      'DELETE FROM audit_log WHERE id = 1',
      'TRUNCATE audit_log',
    ]) {
      await expect(db.exec(sql)).rejects.toThrow('audit_log is append-only');
    }
    const { rows } = await db.query('SELECT action FROM audit_log WHERE id = 1');
    expect(rows).toEqual([{ action: 'obligations.extraction' }]);
  });
});
