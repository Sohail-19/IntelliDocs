import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import type { MigrationBuilder } from 'node-pg-migrate';
import request from 'supertest';
import { afterEach, expect, it } from 'vitest';
import type { DocumentSummary, TaskFeed } from '../../../contracts/core-api.v1.js';

import { createApp } from '../src/app.js';
import { inTransaction, PostgresAuditReader, writeAudit } from '../src/audit.js';
import { orgPolicy } from '../src/auth/policy.js';
import { PostgresAuthRepository } from '../src/auth/repository.js';
import { AuthService } from '../src/auth/service.js';
import { loadNamedOrgConfigs } from '../src/config/loader.js';
import { PostgresDocumentRepository } from '../src/documents.js';
import { IntelligenceClient } from '../src/intelligence/client.js';
import { PostgresObligationRepository } from '../src/obligations.js';
import { PostgresRelationRepository } from '../src/relations/repository.js';
import { configuredRouting } from '../src/routing/config.js';
import { PostgresRoutingService } from '../src/routing/service.js';
import { TaskRepository } from '../src/tasks/repository.js';
import { createTestDatabase } from './postgres.js';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const configPath = join(root, 'config', 'orgs');
const python = process.env.ONBOARDING_INTELLIGENCE_PYTHON;
const cleanup: (() => Promise<unknown>)[] = [];
const bootstrap = 'onboarding-bootstrap-fixture';
const password = 'onboarding-password-fixture';
const email = 'reader@example.test';
const fixture = JSON.parse(
  await readFile(join(root, 'e2e', 'onboarding-fixtures.json'), 'utf8'),
) as Record<
  'example' | 'example2',
  {
    language: string;
    department: string;
    doc_type: string;
    quote: string;
    action: string;
    due_date: string;
    date: string;
    additional_quote?: string;
    additional_point?: string;
  }
>;

afterEach(async () => {
  for (const dispose of cleanup.splice(0).reverse()) await dispose();
});

async function intelligenceUrl(executable: string) {
  const storage = await mkdtemp(join(tmpdir(), 'intellidocs-onboarding-'));
  cleanup.push(() => rm(storage, { recursive: true, force: true }));
  const child = spawn(executable, [join(root, 'e2e', 'onboarding_intelligence.py')], {
    env: {
      ...process.env,
      ORG_CONFIG_DIR: configPath,
      STORAGE_DIR: storage,
      RETRIEVAL_MODE: 'hybrid',
      VERIFICATION_SUPPORT_CHECK: 'false',
      EXTRACTION_CONFIDENCE_WEIGHTS: '',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.setEncoding('utf8').on('data', (data: string) => {
    stderr += data;
  });
  cleanup.push(async () => {
    if (child.exitCode === null) {
      child.kill();
      await new Promise((resolve) => child.once('close', resolve));
    }
  });
  const lines = createInterface(child.stdout);
  const port = await new Promise<string>((resolve, reject) => {
    lines.once('line', resolve);
    child
      .once('error', reject)
      .once('close', () => reject(new Error(stderr || 'Intelligence process closed')));
  });
  const url = 'http://127.0.0.1:' + port;
  for (let attempt = 0; attempt < 50; attempt++) {
    if (
      await fetch(url + '/health').then(
        (response) => response.ok,
        () => false,
      )
    )
      return url;
    await delay(50);
  }
  throw new Error(stderr || 'Intelligence process did not become ready');
}

async function setup(url = 'http://unused.test') {
  const database = createTestDatabase();
  cleanup.push(() => database.db.close());
  const migrations = new URL('../migrations/', import.meta.url);
  for (const filename of (await readdir(migrations)).sort()) {
    if (!/^\d+_.*\.ts$/.test(filename)) continue;
    const module = (await import(new URL(filename, migrations).href)) as {
      up: (builder: MigrationBuilder) => void;
    };
    await database.apply(module.up);
  }
  const configs = new Map(
    (await loadNamedOrgConfigs(configPath)).map(({ slug, config }) => [slug, config]),
  );
  const users = new PostgresAuthRepository(database.pool);
  const policies = new Map();
  const engines = new Map();
  const review = new Map<string, string>();
  for (const [slug, config] of configs) {
    const policy = orgPolicy(slug, config);
    const engine = configuredRouting(config);
    if (!policy || !engine || !config.routing_policy)
      throw new Error('Missing onboarding configuration');
    policies.set(slug, policy);
    engines.set(slug, engine);
    review.set(slug, config.routing_policy.review_role);
    await users.seed(slug, config.departments, Object.keys(config.roles ?? {}));
  }
  const auth = await AuthService.create(users, policies, {
    jwtSecret: Buffer.alloc(32, 19),
    bootstrapHash: createHash('sha256').update(bootstrap).digest('hex'),
    accessSeconds: 300,
    refreshSeconds: 600,
    rateLimit: 30,
  });
  const documents = new PostgresDocumentRepository(database.pool);
  const routing = new PostgresRoutingService(database.pool, engines);
  const client = request(
    createApp(
      {
        intelligence: new IntelligenceClient(url),
        documents,
        orgConfigs: configs,
        documentBrowser: { reader: documents, configs },
        obligations: new PostgresObligationRepository(database.pool, routing),
        routing,
        tasks: new TaskRepository(database.pool, review),
        relations: new PostgresRelationRepository(database.pool),
        audit: new PostgresAuditReader(database.pool),
        summaryPreviewAudit: (context, event) =>
          inTransaction(database.pool, (transaction) => writeAudit(transaction, context, event)),
      },
      { auth },
    ),
  );
  return { ...database, client };
}

type Client = Awaited<ReturnType<typeof setup>>['client'];
async function register(
  client: Client,
  org_id: 'example' | 'example2',
  department: string,
  account = email,
  grant?: { token: string; role: string },
) {
  const call = client.post('/api/auth/register');
  if (grant) call.set('Authorization', grant.token);
  const created = await call.send({
    org_id,
    department,
    email: account,
    password,
    name: 'Fixture reader',
    ...(grant ? { roles: [grant.role] } : { bootstrap_token: bootstrap }),
  });
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const login = await client.post('/api/auth/login').send({ org_id, email: account, password });
  expect(login.status).toBe(200);
  return 'Bearer ' + (login.body as { access_token: string }).access_token;
}

it.runIf(Boolean(python))(
  'onboards both orgs on one unchanged stack and routes a Spanish obligation into role-aware summaries',
  async () => {
    const upstream = await intelligenceUrl(python!);
    const { client, db } = await setup(upstream);
    const ids = new Map<string, string>();
    const tokens = new Map<string, string>();
    for (const org of ['example', 'example2'] as const) {
      const item = fixture[org];
      const token = await register(client, org, item.department);
      tokens.set(org, token);
      const message = [
        'From: notices@example.test',
        'To: reader@example.test',
        'MIME-Version: 1.0',
        'Content-Type: text/plain; charset=UTF-8',
        'Content-Transfer-Encoding: 8bit',
        '',
        item.quote,
        '',
        item.additional_quote ?? '',
      ].join('\r\n');
      const upload = await client
        .post('/v1/upload')
        .set('Authorization', token)
        .field('department', item.department)
        .field('doc_type', item.doc_type)
        .field('doc_date', item.date)
        .attach('file', Buffer.from(message), {
          filename: org + '.eml',
          contentType: 'message/rfc822',
        });
      expect(upload.status, JSON.stringify(upload.body)).toBe(202);
      expect(upload.body).toMatchObject({
        parse_result: { org_id: org, document: { pages: [{ language: item.language }] } },
        extraction_results: [
          {
            obligations: [
              {
                action: item.action,
                status: 'verified',
                owner: { resolved_department: item.department },
                deadline: { normalized_date: item.due_date },
              },
            ],
          },
        ],
      });
      ids.set(org, (upload.body as { document_id: string }).document_id);
    }
    const doc = ids.get('example2')!;
    const manager = tokens.get('example2')!;
    const inspector = await register(
      client,
      'example2',
      'water_quality',
      'inspector@example.test',
      { token: manager, role: 'quality_inspector' },
    );
    const liaison = await register(client, 'example2', 'customer_service', 'liaison@example.test', {
      token: manager,
      role: 'resident_liaison',
    });
    const tasks = await client.get('/api/tasks').set('Authorization', inspector);
    expect(tasks.body).toMatchObject({
      tasks: [
        {
          doc_id: doc,
          action: fixture.example2.action,
          due_date: fixture.example2.due_date,
          queue_name: 'quality_inspector',
        },
      ],
    });
    const quality = await client
      .get(`/api/documents/${doc}/summary`)
      .set('Authorization', inspector);
    expect(quality.status, JSON.stringify(quality.body)).toBe(200);
    expect(quality.body).toMatchObject({
      summary: {
        audience: { department: 'water_quality', role: 'quality_inspector' },
        language: 'es',
        points: [
          { due_date: fixture.example2.due_date, citations: [{ quote: fixture.example2.quote }] },
        ],
      },
    });
    const publicInfo = await client
      .get(`/api/documents/${doc}/summary`)
      .set('Authorization', liaison);
    expect((publicInfo.body as DocumentSummary).summary?.points.map(({ text }) => text)).toEqual([
      fixture.example2.additional_point,
    ]);
    const filtered = await client
      .get('/api/search')
      .query({ query: fixture.example2.action, top_k: 10, 'filters[org_id]': 'example2' })
      .set('Authorization', tokens.get('example')!);
    expect(filtered.status).toBe(200);
    const results = (filtered.body as { results: { chunk: { org_id: string; doc_id: string } }[] })
      .results;
    expect(results.length).toBeGreaterThan(0);
    expect(
      results.every(
        ({ chunk }) => chunk.org_id === 'example' && chunk.doc_id === ids.get('example'),
      ),
    ).toBe(true);
    const rows = await db.query<{ slug: string; queue_name: string }>(
      'SELECT org.slug, a.queue_name FROM assignments a JOIN organizations org ON org.id = a.org_id ORDER BY a.id',
    );
    expect(rows.rows).toEqual([
      { slug: 'example', queue_name: 'finance' },
      { slug: 'example2', queue_name: 'quality_inspector' },
    ]);
    for (const endpoint of ['index', 'extract-obligations', 'detect-relations']) {
      const rejected = await fetch(`${upstream}/v1/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: doc, org_id: 'example' }),
      });
      expect(rejected.status, endpoint).toBe(404);
    }
    const unscoped = await fetch(`${upstream}/v1/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: 'control de cloro', top_k: 10 }),
    });
    expect(await unscoped.json()).toEqual({ results: [] });
  },
  30_000,
);

it('keeps documents, obligations, tasks, relations and audit records inside the authenticated org', async () => {
  const { client, db } = await setup();
  const a = await register(client, 'example', fixture.example.department);
  const b = await register(client, 'example2', fixture.example2.department);
  for (const org of ['example', 'example2'] as const) {
    const department = fixture[org].department;
    const {
      rows: [tenant],
    } = await db.query<{ id: number }>('SELECT id FROM organizations WHERE slug = $1', [org]);
    if (!tenant) throw new Error('Tenant was not seeded');
    const {
      rows: [target, source],
    } = await db.query<{ id: number }>(
      `INSERT INTO documents (org_id, external_id, filename, status, extraction_status)
       VALUES ($1, $2::text, $2::text || '.eml', 'indexed', 'completed'), ($1, $3::text, $3::text || '.eml', 'indexed', 'completed') RETURNING id`,
      [tenant.id, org + '-private', org + '-newer'],
    );
    if (!target || !source) throw new Error('Could not create test documents');
    const {
      rows: [obligation],
    } = await db.query<{ id: number; org_id: number }>(
      `INSERT INTO obligations (document_id, external_id, action, owner, owner_details, deadline, citations, status, obligation_type, confidence, extractor_version)
       VALUES ($1, $2::text, $2::text, 'fixture', $3::jsonb, $4::jsonb, $5::jsonb, 'verified', 'reporting', 1, 'fixture') RETURNING id, org_id`,
      [
        target.id,
        org + '-duty',
        JSON.stringify({
          raw_text: department,
          resolved_department: department,
          resolved_role: null,
          resolved: true,
        }),
        JSON.stringify({
          raw_text: '',
          normalized_date: null,
          anchor_date: null,
          resolved: false,
          is_relative: false,
        }),
        JSON.stringify([
          { doc_id: org + '-private', page: 1, char_start: 0, char_end: 7, quote: 'fixture' },
        ]),
      ],
    );
    if (!obligation) throw new Error('Could not create a test obligation');
    expect(obligation.org_id).toBe(tenant.id);
    await db.query(
      `INSERT INTO assignments (obligation_id, queue_type, queue_name, reason) VALUES ($1, 'department', $2, 'rule')`,
      [obligation.id, department],
    );
    await db.query(
      `INSERT INTO document_relations (org_id, id, source_id, target_id, relation_type, scope, source_citations, target_citations, confidence, detected_by, created_at)
       VALUES ($1, $2, $3, $4, 'references', 'whole_document', '[]', '[]', 1, 'fixture', NOW())`,
      [tenant.id, org + '-relation', source.id, target.id],
    );
    await db.query(
      `INSERT INTO audit_log (org_id, action, entity_type, entity_id, request_id) VALUES ($1, 'fixture.upload', 'document', $2, $3)`,
      [tenant.id, org + '-private', org + '-request'],
    );
  }
  for (const [token, own, other] of [
    [a, 'example', 'example2'],
    [b, 'example2', 'example'],
  ] as const) {
    const get = (path: string) => client.get(path).set('Authorization', token);
    for (const path of [
      `/api/documents/${other}-private`,
      `/api/documents/${other}-private/source`,
      `/api/documents/${other}-private/obligations`,
      `/api/documents/${other}-private/summary`,
      `/api/relations/${other}-relation`,
    ]) {
      expect((await get(path)).status, path).toBe(404);
    }
    const documents = (await get('/api/documents')).body as { documents: { id: string }[] };
    expect(documents.documents.map(({ id }) => id).sort()).toEqual([
      own + '-newer',
      own + '-private',
    ]);
    expect((await get(`/api/obligations?doc_id=${other}-private`)).body).toEqual({
      obligations: [],
    });
    expect((await get('/api/obligations')).body).toMatchObject({
      obligations: [{ id: own + '-duty' }],
    });
    expect(((await get('/api/tasks')).body as TaskFeed).tasks.map(({ action }) => action)).toEqual([
      own + '-duty',
    ]);
    expect((await get(`/api/tasks?doc_id=${other}-private`)).body).toMatchObject({ tasks: [] });
    expect((await get('/api/relations')).body).toMatchObject({
      relations: [{ id: own + '-relation' }],
    });
    expect((await get('/api/audit?action=fixture.upload')).body).toMatchObject({
      entries: [{ entity_id: own + '-private' }],
    });
    expect((await get(`/api/audit?entity_id=${other}-private`)).body).toMatchObject({
      entries: [],
    });
  }
  const tenants = await db.query<{ id: number; org_id: number }>(
    'SELECT id, org_id FROM obligations ORDER BY id',
  );
  const [first, second] = tenants.rows;
  if (!first || !second) throw new Error('Missing tenant fixtures');
  await expect(
    db.query('UPDATE assignments SET org_id = $1 WHERE obligation_id = $2', [
      second.org_id,
      first.id,
    ]),
  ).rejects.toThrow('assignments_tenant_obligation_fk');
}, 30_000);
