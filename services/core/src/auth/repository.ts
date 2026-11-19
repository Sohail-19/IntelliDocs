import type { Pool, PoolClient } from 'pg';

import { inTransaction, writeAudit } from '../audit.js';
import type { AuditContext, AuditRequest } from '../audit.js';
import type { OrgPolicy, Principal } from './policy.js';

export interface Account extends Principal {
  passwordHash: string;
}

export interface NewUser {
  email: string;
  name: string;
  department?: string;
  passwordHash: string;
  roles: string[];
  org: OrgPolicy;
  bootstrapAuthorized: boolean;
  administratorAuthorized: boolean;
  audit: AuditRequest;
  actorUserId: number | null;
}

export interface RefreshRecord {
  hash: string;
  expiresAt: Date;
}

export interface AuthRepository {
  register(input: NewUser): Promise<'created' | 'forbidden' | 'duplicate'>;
  account(orgSlug: string, email: string): Promise<Account | undefined>;
  principal(id: number): Promise<Principal | undefined>;
  saveRefresh(user: Principal, token: RefreshRecord, audit: AuditRequest): Promise<void>;
  rotateRefresh(
    hash: string,
    now: Date,
    replacement: RefreshRecord,
    audit: AuditRequest,
  ): Promise<Principal | undefined>;
  replaceRoles(
    userId: number,
    roles: string[],
    administratorRoles: string[],
    audit: AuditContext,
  ): Promise<'updated' | 'missing' | 'last_admin'>;
}

const principalFields =
  'u.id, u.org_id AS "orgId", o.slug AS "orgSlug", ' +
  'ARRAY(SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id ' +
  '      WHERE ur.user_id = u.id ORDER BY r.name) AS roles';
const activeUsers = ' FROM users u JOIN organizations o ON o.id = u.org_id WHERE u.active AND ';
const principalQuery = 'SELECT ' + principalFields + activeUsers;

export class PostgresAuthRepository implements AuthRepository {
  constructor(private readonly pool: Pool) {}

  async seed(slug: string, departments: string[], roles: string[]): Promise<void> {
    await inTransaction(this.pool, async (client) => {
      const { rows } = await client.query<{ id: number }>(
        'INSERT INTO organizations (slug) VALUES ($1) ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id',
        [slug],
      );
      const id = rows[0]?.id;
      if (id === undefined) throw new Error('Organization seed failed');
      await client.query(
        'INSERT INTO departments (org_id, name) SELECT $1, name FROM unnest($2::text[]) AS name ON CONFLICT (org_id, name) DO NOTHING',
        [id, departments],
      );
      await client.query(
        'INSERT INTO roles (org_id, name) SELECT $1, name FROM unnest($2::text[]) AS name ON CONFLICT (org_id, name) DO NOTHING',
        [id, roles],
      );
    });
  }

  async register(input: NewUser): Promise<'created' | 'forbidden' | 'duplicate'> {
    return inTransaction(this.pool, async (client) => {
      // Lock the organization so two requests cannot both claim the first account.
      const { rows } = await client.query<{ id: number }>(
        'SELECT id FROM organizations WHERE slug = $1 FOR UPDATE',
        [input.org.slug],
      );
      const orgId = rows[0]?.id;
      if (orgId === undefined) return 'forbidden';
      const { rows: users } = await client.query('SELECT id FROM users WHERE org_id = $1 LIMIT 1', [
        orgId,
      ]);
      const first = users.length === 0;
      if (first ? !input.bootstrapAuthorized : !input.administratorAuthorized) return 'forbidden';
      const { rows: created } = await client.query<{ id: number }>(
        'INSERT INTO users (org_id, department_id, email, password_hash, name, active) VALUES ' +
          '($1, (SELECT id FROM departments WHERE org_id = $1 AND name = $2), $3, $4, $5, true) ' +
          'ON CONFLICT (org_id, email) DO NOTHING RETURNING id',
        [orgId, input.department ?? null, input.email, input.passwordHash, input.name],
      );
      const userId = created[0]?.id;
      if (userId === undefined) return 'duplicate';
      const roles = first ? [input.org.bootstrapRole] : input.roles;
      await this.insertRoles(client, orgId, userId, roles);
      await writeAudit(
        client,
        { ...input.audit, orgId, actorUserId: input.actorUserId },
        {
          action: 'user.register',
          entityType: 'user',
          entityId: String(userId),
          before: null,
          after: { active: true, department: input.department ?? null, roles },
        },
      );
      return 'created';
    });
  }

  async account(orgSlug: string, email: string): Promise<Account | undefined> {
    const { rows } = await this.pool.query<Account>(
      'SELECT u.password_hash AS "passwordHash", ' +
        principalFields +
        activeUsers +
        'o.slug = $1 AND u.email = $2',
      [orgSlug, email],
    );
    return rows[0];
  }

  async principal(id: number): Promise<Principal | undefined> {
    const { rows } = await this.pool.query<Principal>(principalQuery + 'u.id = $1', [id]);
    return rows[0];
  }

  async saveRefresh(user: Principal, token: RefreshRecord, audit: AuditRequest): Promise<void> {
    await inTransaction(this.pool, async (client) => {
      await this.insertRefresh(client, user.id, token);
      await writeAudit(
        client,
        { ...audit, orgId: user.orgId, actorUserId: user.id },
        {
          action: 'auth.login',
          entityType: 'user',
          entityId: String(user.id),
          before: null,
          after: { session_created: true, expires_at: token.expiresAt },
        },
      );
    });
  }

  async rotateRefresh(
    hash: string,
    now: Date,
    replacement: RefreshRecord,
    audit: AuditRequest,
  ): Promise<Principal | undefined> {
    return inTransaction(this.pool, async (client) => {
      const { rows } = await client.query<{ user_id: number; expires_at: Date }>(
        'DELETE FROM refresh_tokens WHERE token_hash = $1 AND expires_at > $2 RETURNING user_id, expires_at',
        [hash, now],
      );
      const token = rows[0];
      if (!token) return undefined;
      const { rows: users } = await client.query<Principal>(principalQuery + 'u.id = $1', [
        token.user_id,
      ]);
      const user = users[0];
      if (!user) {
        // Preserve the old token row for a denied refresh; inactive accounts cannot use it.
        await this.insertRefresh(client, token.user_id, { hash, expiresAt: token.expires_at });
        return undefined;
      }
      await this.insertRefresh(client, user.id, replacement);
      await writeAudit(
        client,
        { ...audit, orgId: user.orgId, actorUserId: user.id },
        {
          action: 'auth.refresh',
          entityType: 'user',
          entityId: String(user.id),
          before: { expires_at: token.expires_at },
          after: { expires_at: replacement.expiresAt },
        },
      );
      return user;
    });
  }

  private async insertRefresh(
    client: PoolClient,
    userId: number,
    token: RefreshRecord,
  ): Promise<void> {
    await client.query(
      'INSERT INTO refresh_tokens (token_hash, user_id, expires_at) VALUES ($1, $2, $3)',
      [token.hash, userId, token.expiresAt],
    );
  }

  async replaceRoles(
    userId: number,
    roles: string[],
    administratorRoles: string[],
    audit: AuditContext,
  ): Promise<'updated' | 'missing' | 'last_admin'> {
    return inTransaction(this.pool, async (client) => {
      await client.query('SELECT id FROM organizations WHERE id = $1 FOR UPDATE', [audit.orgId]);
      const { rows } = await client.query<Principal>(
        principalQuery + 'u.id = $1 AND u.org_id = $2',
        [userId, audit.orgId],
      );
      const user = rows[0];
      if (!user) return 'missing';
      if (
        user.roles.some((role) => administratorRoles.includes(role)) &&
        !roles.some((role) => administratorRoles.includes(role))
      ) {
        const otherAdmins = await client.query(
          'SELECT 1 FROM users u JOIN user_roles ur ON u.id = ur.user_id JOIN roles r ON r.id = ur.role_id ' +
            'WHERE u.org_id = $1 AND u.active AND u.id <> $2 AND r.name = ANY($3::text[]) LIMIT 1',
          [audit.orgId, userId, administratorRoles],
        );
        if (!otherAdmins.rowCount) return 'last_admin';
      }
      await client.query('DELETE FROM user_roles WHERE user_id = $1 AND org_id = $2', [
        userId,
        audit.orgId,
      ]);
      await this.insertRoles(client, audit.orgId, userId, roles);
      await writeAudit(client, audit, {
        action: 'user.roles',
        entityType: 'user',
        entityId: String(userId),
        before: { roles: user.roles },
        after: { roles },
      });
      return 'updated';
    });
  }

  private async insertRoles(
    client: PoolClient,
    orgId: number,
    userId: number,
    roles: string[],
  ): Promise<void> {
    const assigned = await client.query(
      'INSERT INTO user_roles (org_id, user_id, role_id) ' +
        'SELECT $1, $2, id FROM roles WHERE org_id = $1 AND name = ANY($3::text[])',
      [orgId, userId, roles],
    );
    if (assigned.rowCount !== roles.length) throw new Error('Role assignment failed');
  }
}
