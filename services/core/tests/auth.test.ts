import { createHash, randomBytes } from 'node:crypto';

import express from 'express';
import request from 'supertest';
import { describe, expect, it, vi } from 'vitest';

import { createApp } from '../src/app.js';
import type { OrgPolicy, Principal } from '../src/auth/policy.js';
import type { Account, AuthRepository, NewUser, RefreshRecord } from '../src/auth/repository.js';
import { requireRole } from '../src/auth/routes.js';
import { AuthService } from '../src/auth/service.js';

const password = 'test password of sufficient length';
const credentials = { org_id: 'example', email: 'admin@example.test', password };
const viewer = { org_id: 'example', email: 'viewer@example.test', password };
const adminRole = 'configured_admin';
const readerRole = 'configured_reader';
const org: OrgPolicy = {
  slug: 'example',
  departments: ['research'],
  roles: new Map([
    [adminRole, ['users:register', 'documents:search', 'documents:upload']],
    [readerRole, ['documents:search']],
  ]),
  bootstrapRole: adminRole,
  registrationRole: readerRole,
};

class MemoryUsers implements AuthRepository {
  accounts = new Map<number, Account>();
  tokens = new Map<string, RefreshRecord & { userId: number }>();

  register(input: NewUser): Promise<'created' | 'forbidden' | 'duplicate'> {
    const first = ![...this.accounts.values()].some(
      (account) => account.orgSlug === input.org.slug,
    );
    if (first ? !input.bootstrapAuthorized : !input.administratorAuthorized) {
      return Promise.resolve('forbidden');
    }
    if (this.findAccount(input.org.slug, input.email)) return Promise.resolve('duplicate');
    const id = this.accounts.size + 1;
    this.accounts.set(id, {
      id,
      orgId: input.org.slug === 'example' ? 1 : 2,
      orgSlug: input.org.slug,
      roles: first ? [input.org.bootstrapRole] : input.roles,
      passwordHash: input.passwordHash,
    });
    this.emails.set(id, input.email);
    return Promise.resolve('created');
  }

  private emails = new Map<number, string>();

  private findAccount(slug: string, email: string): Account | undefined {
    return [...this.accounts.values()].find(
      (account) => account.orgSlug === slug && this.emails.get(account.id) === email,
    );
  }

  account(slug: string, email: string): Promise<Account | undefined> {
    return Promise.resolve(this.findAccount(slug, email));
  }

  principal(id: number): Promise<Principal | undefined> {
    return Promise.resolve(this.accounts.get(id));
  }

  saveRefresh(user: Principal, token: RefreshRecord): Promise<boolean> {
    this.tokens.set(token.hash, { ...token, userId: user.id });
    return Promise.resolve(true);
  }

  async rotateRefresh(
    old: string,
    now: Date,
    token: RefreshRecord,
  ): Promise<Principal | undefined> {
    const record = this.tokens.get(old);
    if (!record || record.expiresAt <= now) return undefined;
    this.tokens.delete(old);
    const user = await this.principal(record.userId);
    if (user) await this.saveRefresh(user, token);
    return user;
  }
  revokeRefresh(hash: string): Promise<void> {
    this.tokens.delete(hash);
    return Promise.resolve();
  }
  replaceRoles(): Promise<'missing'> {
    return Promise.resolve('missing');
  }
  listUsers(): Promise<[]> {
    return Promise.resolve([]);
  }
  changeUser(): Promise<'missing'> {
    return Promise.resolve('missing');
  }
}

function digest(token: string): string {
  return createHash('sha256').update(token).digest('hex');
}

async function setup(rateLimit = 30) {
  const users = new MemoryUsers();
  const bootstrap = randomBytes(32).toString('base64url');
  let now = new Date('2026-01-01T00:00:00Z');
  const auth = await AuthService.create(
    users,
    new Map([
      ['example', org],
      ['another', { ...org, slug: 'another' }],
    ]),
    {
      jwtSecret: randomBytes(32),
      bootstrapHash: digest(bootstrap),
      accessSeconds: 30,
      refreshSeconds: 3600,
      rateLimit,
    },
    () => now,
  );
  const search = vi.fn().mockResolvedValue({ results: [] });
  const app = createApp(
    {
      intelligence: { ingest: vi.fn(), index: vi.fn(), search },
      documents: { save: vi.fn(), setStatus: vi.fn() },
    },
    { auth },
  );
  const roleProbe = express();
  roleProbe.get('/admin', requireRole(auth, org.bootstrapRole), (_request, response) => {
    response.sendStatus(204);
  });
  const client = request(app);
  const register = () =>
    client.post('/api/auth/register').send({
      ...credentials,
      name: 'Admin',
      bootstrap_token: bootstrap,
      roles: [readerRole],
      department: 'research',
    });
  async function login(account = credentials) {
    const response = await client.post('/api/auth/login').send(account);
    expect(response.status).toBe(200);
    return response.body as { access_token: string; refresh_token: string; expires_in: number };
  }
  return {
    client,
    roleProbe,
    register,
    login,
    users,
    bootstrap,
    search,
    advance: (seconds: number) => {
      now = new Date(now.getTime() + seconds * 1000);
    },
  };
}

it('keeps production refresh tokens in secured cookies and rejects foreign origins', async () => {
  const origin = 'https://app.example.test';
  vi.stubEnv('INTELLIDOCS_ENV', 'production');
  vi.stubEnv('INTELLIDOCS_INTERNAL_KEY', randomBytes(32).toString('hex'));
  vi.stubEnv('CORE_CORS_ORIGINS', origin);
  try {
    const { client, register } = await setup();
    await register().expect(201);
    await client
      .post('/api/auth/login')
      .set('Origin', 'https://other.example.test')
      .send(credentials)
      .expect(403);
    const login = await client
      .post('/api/auth/login')
      .set('Origin', origin)
      .send(credentials)
      .expect(200);
    expect(login.body).not.toHaveProperty('refresh_token');
    expect(login.headers['access-control-allow-credentials']).toBe('true');
    const [set] = login.headers['set-cookie'] as string[];
    expect(set).toMatch(/^__Host-intellidocs-refresh=/);
    for (const flag of ['Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'])
      expect(set).toContain(flag);
    const [cookie] = set!.split(';');
    await client.post('/api/auth/refresh').set('Cookie', cookie!).expect(403);
    const refreshed = await client
      .post('/api/auth/refresh')
      .set('Origin', origin)
      .set('Cookie', cookie!)
      .send({})
      .expect(200);
    expect(refreshed.body).not.toHaveProperty('refresh_token');
    await client
      .post('/api/auth/refresh')
      .set('Origin', origin)
      .set('Cookie', cookie!)
      .send({})
      .expect(401);
  } finally {
    vi.unstubAllEnvs();
  }
});

describe('core auth', () => {
  it('bootstraps the configured admin, then requires an admin in the same org', async () => {
    const { client, register, bootstrap, login, users } = await setup();
    const plain = { ...credentials, name: 'Admin' };
    expect((await client.post('/api/auth/register').send(plain)).status).toBe(403);
    expect((await register()).status).toBe(201);
    expect(users.accounts.get(1)?.roles).toEqual([adminRole]);
    expect(users.accounts.get(1)?.passwordHash).toMatch(/^\$argon2id\$/);
    expect(
      (
        await client.post('/api/auth/register').send({
          ...viewer,
          name: 'Viewer',
          bootstrap_token: bootstrap,
        })
      ).status,
    ).toBe(403);
    const admin = await login();
    const create = await client
      .post('/api/auth/register')
      .set('Authorization', 'Bearer ' + admin.access_token)
      .send({ ...viewer, name: 'Viewer' });
    expect(create.status).toBe(201);
    expect(users.accounts.get(2)?.roles).toEqual([readerRole]);
    const member = await login(viewer);
    expect(
      (
        await client
          .post('/api/auth/register')
          .set('Authorization', 'Bearer ' + member.access_token)
          .send({ ...viewer, name: 'Viewer' })
      ).status,
    ).toBe(403);
    expect(
      (
        await client
          .post('/api/auth/register')
          .set('Authorization', 'Bearer ' + admin.access_token)
          .send({ ...plain, org_id: 'another' })
      ).status,
    ).toBe(403);
  });

  it('rejects wrong passwords and expired tokens, and applies role grants and org scope', async () => {
    const { client, roleProbe, register, login, advance, search } = await setup();
    expect((await client.get('/api/search')).status).toBe(401);
    expect((await register()).status).toBe(201);
    expect(
      (await client.post('/api/auth/login').send({ ...credentials, password: 'wrong' })).status,
    ).toBe(401);
    const admin = await login();
    const adminHeader = 'Bearer ' + admin.access_token;
    expect((await request(roleProbe).get('/admin').set('Authorization', adminHeader)).status).toBe(
      204,
    );
    expect(
      (
        await client
          .post('/api/auth/register')
          .set('Authorization', adminHeader)
          .send({ ...viewer, name: 'Viewer' })
      ).status,
    ).toBe(201);
    const member = await login(viewer);
    const memberHeader = 'Bearer ' + member.access_token;
    expect((await request(roleProbe).get('/admin').set('Authorization', memberHeader)).status).toBe(
      403,
    );
    expect((await client.post('/v1/upload').set('Authorization', memberHeader)).status).toBe(403);
    const allowed = await client
      .get('/api/search')
      .set('Authorization', memberHeader)
      .query({ query: 'report', top_k: 1, org_id: 'another' });
    expect(allowed.status).toBe(200);
    expect(search).toHaveBeenCalledWith({
      query: 'report',
      top_k: 1,
      filters: { org_id: 'example' },
    });
    advance(admin.expires_in);
    expect((await client.get('/api/search').set('Authorization', adminHeader)).status).toBe(401);
  });

  it('rotates refresh tokens and stores only digests', async () => {
    const { client, register, login, users, advance } = await setup();
    expect((await register()).status).toBe(201);
    const session = await login();
    expect([...users.tokens.keys()]).toEqual([digest(session.refresh_token)]);
    advance(session.expires_in);
    const attempts = await Promise.all(
      [1, 2].map(() =>
        client.post('/api/auth/refresh').send({ refresh_token: session.refresh_token }),
      ),
    );
    expect(attempts.map((response) => response.status).sort()).toEqual([200, 401]);
    const successful = attempts.find((response) => response.status === 200)?.body as typeof session;
    expect(successful.refresh_token).not.toBe(session.refresh_token);
    expect([...users.tokens.keys()]).toEqual([digest(successful.refresh_token)]);
    expect(
      (
        await client
          .get('/api/search')
          .set('Authorization', 'Bearer ' + successful.access_token)
          .query({ query: 'report', top_k: 1 })
      ).status,
    ).toBe(200);
    advance(3600);
    expect(
      (await client.post('/api/auth/refresh').send({ refresh_token: successful.refresh_token }))
        .status,
    ).toBe(401);
  });

  it('reports the current grants from the organization policy', async () => {
    const { client, register, login } = await setup();
    expect((await client.get('/api/auth/me')).status).toBe(401);
    expect((await register()).status).toBe(201);
    const session = await login();
    const response = await client
      .get('/api/auth/me')
      .set('Authorization', 'Bearer ' + session.access_token);
    expect(response.status).toBe(200);
    expect(response.body).toEqual({
      user_id: 1,
      org_id: 'example',
      roles: [adminRole],
      permissions: ['documents:search', 'documents:upload', 'users:register'],
    });
  });

  it.each([
    ['development', 'http://127.0.0.1:55173'],
    ['production', 'https://web.example.test'],
  ])('allows credentialed login only from configured origins in %s', async (profile, origin) => {
    vi.stubEnv('INTELLIDOCS_ENV', profile);
    vi.stubEnv('INTELLIDOCS_INTERNAL_KEY', randomBytes(32).toString('hex'));
    vi.stubEnv('CORE_CORS_ORIGINS', 'https://other.example.test, ' + origin);
    try {
      const { client, register } = await setup();
      const preflight = (from: string) =>
        client
          .options('/api/auth/login')
          .set('Origin', from)
          .set('Access-Control-Request-Method', 'POST')
          .set('Access-Control-Request-Headers', 'content-type');
      const login = (from: string) =>
        client.post('/api/auth/login').set('Origin', from).send(credentials);
      await register().expect(201);
      const allowed = await preflight(origin).expect(204);
      expect(allowed.headers['access-control-allow-headers']).toContain('Content-Type');
      expect(allowed.headers['access-control-allow-headers']).toContain('Authorization');
      for (const response of [allowed, await login(origin).expect(200)]) {
        expect(response.headers['access-control-allow-origin']).toBe(origin);
        expect(response.headers['access-control-allow-credentials']).toBe('true');
        expect(response.headers.vary).toContain('Origin');
      }

      const foreign = 'https://web.example.test.unlisted.test';
      for (const response of [await preflight(foreign), await login(foreign)]) {
        expect(
          Object.keys(response.headers).filter((key) => key.startsWith('access-control-')),
        ).toEqual([]);
        expect(response.headers.vary).toContain('Origin');
        if (profile === 'production') expect(response.status).toBe(403);
      }
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it.each(['*', 'https://web.example.test,*', 'https://*.example.test'])(
    'rejects wildcard CORS configuration %s at startup',
    (origins) => {
      vi.stubEnv('CORE_CORS_ORIGINS', origins);
      try {
        expect(() => createApp()).toThrow(
          'CORE_CORS_ORIGINS cannot contain wildcards when credentials are allowed',
        );
      } finally {
        vi.unstubAllEnvs();
      }
    },
  );

  it('adds security headers and rate limits authentication requests', async () => {
    const { client } = await setup(1);
    const response = await client.post('/api/auth/login').send(credentials);
    expect(response.status).toBe(401);
    expect(response.headers['cache-control']).toBe('no-store');
    expect(response.headers['x-content-type-options']).toBe('nosniff');
    expect((await client.post('/api/auth/login').send(credentials)).status).toBe(429);
  });
});
