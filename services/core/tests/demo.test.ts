import { randomBytes } from 'node:crypto';
import { readdirSync } from 'node:fs';
import type { MigrationBuilder } from 'node-pg-migrate';
import request from 'supertest';
import { afterEach, expect, it, vi } from 'vitest';

import { createApp } from '../src/app.js';
import { authSettingsFromEnv } from '../src/auth/service.js';
import { DemoCleanup, demoUploadAdmission, reserveUpload } from '../src/demo.js';
import { IntelligenceClient } from '../src/intelligence/client.js';
import { PostgresRelationRepository } from '../src/relations/repository.js';
import { runtimeFromEnv } from '../src/runtime.js';
import { createTestDatabase } from './postgres.js';
import { scopedApp } from './scoped-app.js';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

async function database() {
  const state = createTestDatabase();
  try {
    for (const name of readdirSync(new URL('../migrations/', import.meta.url))
      .filter((name) => name.endsWith('.ts'))
      .sort()) {
      const { up } = (await import(`../migrations/${name}`)) as {
        up(this: void, builder: MigrationBuilder): void;
      };
      await state.apply(up);
    }
    return state;
  } catch (error) {
    await state.db.close();
    throw error;
  }
}

it('rolls back both reservations when either daily upload limit denies the attempt', async () => {
  const { db, pool } = await database();
  try {
    await reserveUpload(pool, 1, 2, 1);
    await expect(reserveUpload(pool, 1, 2, 1)).rejects.toThrow();
    await reserveUpload(pool, 2, 2, 1);
    await expect(reserveUpload(pool, 3, 2, 1)).rejects.toThrow();
    expect(
      (await db.query('SELECT actor_id, used FROM demo_upload_usage ORDER BY actor_id')).rows,
    ).toEqual([
      { actor_id: 0, used: 2 },
      { actor_id: 1, used: 1 },
      { actor_id: 2, used: 1 },
    ]);
  } finally {
    await db.close();
  }
});

it('rejects over-quota uploads before buffering or forwarding them', async () => {
  const { db, pool } = await database();
  try {
    const ingest = vi.fn();
    const app = scopedApp({
      intelligence: { ingest, index: vi.fn() },
      documents: { save: vi.fn(), setStatus: vi.fn() },
      uploadAdmission: demoUploadAdmission(pool, {
        ...runtimeFromEnv(),
        concurrentUploads: 1,
        uploadsGlobal: 10,
        uploadsPerUser: 1,
      }),
    });
    // An authenticated invalid submission consumes the first allowance.
    await request(app).post('/v1/upload').expect(400);
    await request(app)
      .post('/v1/upload')
      .attach('file', Buffer.from('file'), 'fixture.pdf')
      .expect(429);
    expect(ingest).not.toHaveBeenCalled();
  } finally {
    await db.close();
  }
});

it('cleans up only expired demo documents and their attachments', async () => {
  const { db, pool } = await database();
  try {
    const {
      rows: [{ id }],
    } = await db.query<{ id: number }>(
      "INSERT INTO organizations(slug) VALUES ('demo') RETURNING id",
    );
    await db.query(
      `INSERT INTO documents(external_id, filename, org_id, demo_upload, created_at, status) VALUES
      ('old','demo.pdf',$1,true,'2026-01-01','indexed'), ('new','demo.pdf',$1,true,'2026-01-11','indexed'),
      ('permanent','permanent.pdf',$1,false,'2026-01-01','indexed')`,
      [id],
    );
    await db.query(`INSERT INTO documents(external_id, filename, org_id, demo_upload, created_at, parent_document_id, status)
      SELECT 'attachment', 'attach.pdf', org_id, true, created_at, id, 'indexed' FROM documents WHERE external_id='old'`);
    const purge = vi.fn().mockResolvedValue(undefined);
    const cleanup = new DemoCleanup(pool, purge, new PostgresRelationRepository(pool), 7);
    const sweepDate = new Date('2026-01-12T00:00:00Z');
    purge.mockRejectedValueOnce(new Error('temporary storage outage'));
    await cleanup.sweep(sweepDate);
    expect(
      (await db.query("SELECT external_id FROM documents WHERE external_id='old'")).rows,
    ).toEqual([{ external_id: 'old' }]);
    purge.mockClear();
    await cleanup.sweep(sweepDate);
    expect(purge.mock.calls).toEqual([
      ['old', 'demo'],
      ['attachment', 'demo'],
    ]);
    expect((await db.query('SELECT external_id FROM documents ORDER BY external_id')).rows).toEqual(
      [{ external_id: 'new' }, { external_id: 'permanent' }],
    );
  } finally {
    await db.close();
  }
});

it('fails production configuration with absent or default secrets', () => {
  vi.stubEnv('INTELLIDOCS_ENV', 'production');
  vi.stubEnv('INTELLIDOCS_INTERNAL_KEY', randomBytes(32).toString('hex'));
  vi.stubEnv('AUTH_BOOTSTRAP_TOKEN', randomBytes(32).toString('hex'));
  vi.stubEnv('AUTH_JWT_SECRET', '');
  expect(() => authSettingsFromEnv()).toThrow('AUTH_JWT_SECRET');
  vi.stubEnv('AUTH_JWT_SECRET', 'change-me-default-jwt-secret-for-production');
  expect(() => authSettingsFromEnv()).toThrow('AUTH_JWT_SECRET');
  vi.stubEnv('CORE_CORS_ORIGINS', 'http://localhost:4000');
  expect(() => createApp()).toThrow('CORE_CORS_ORIGINS');
});

it('kills LLM calls without calling the provider service and keeps readiness checks', async () => {
  vi.stubEnv('LLM_ENABLED', 'false');
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
  const client = new IntelligenceClient('http://internal.test', fetch);
  const app = scopedApp({
    intelligence: client,
    documents: { save: vi.fn(), setStatus: vi.fn() },
    readiness: {
      postgres: vi.fn().mockRejectedValue(new Error('private database details')),
      intelligence: client.ready.bind(client),
    },
  });
  const answer = await request(app)
    .get('/api/answer')
    .query({ query: 'question', language: 'en', top_k: '3' })
    .expect(503);
  expect(answer.body).toEqual({ error: 'LLM features are disabled by the operator.' });
  expect(fetch).not.toHaveBeenCalled();
  await request(app).get('/health').expect(200);
  const ready = await request(app).get('/ready').expect(503);
  expect(ready.body).toEqual({
    status: 'not_ready',
    checks: { postgres: 'unavailable', intelligence: 'ok' },
  });
});

it('emits request IDs without logging planted credentials, document text, or paths', async () => {
  const planted = randomBytes(24).toString('hex');
  const logs = vi.spyOn(console, 'log').mockImplementation(() => undefined);
  const response = await request(createApp())
    .post(`/${planted}?query=${planted}`)
    .set('Authorization', `Bearer ${planted}`)
    .set('X-Request-ID', planted)
    .send({ document: planted, password: planted })
    .expect(404);
  expect(JSON.stringify(logs.mock.calls)).not.toContain(planted);
  expect(logs).toHaveBeenCalledWith(
    JSON.stringify({
      service: 'core',
      event: 'request',
      request_id: response.headers['x-request-id'] as string,
      method: 'POST',
      status: 404,
    }),
  );
});
