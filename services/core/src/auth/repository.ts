import type { Pool, PoolClient } from 'pg';
import type { UserChange, UserRecord } from '../../../../contracts/core-api.v1.js';

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
  saveRefresh(user: Principal, token: RefreshRecord, audit: AuditRequest): Promise<boolean>;
  rotateRefresh(
    hash: string,
    now: Date,
    replacement: RefreshRecord,
    audit: AuditRequest,
  ): Promise<Principal | undefined>;
  revokeRefresh(hash: string, audit: AuditRequest): Promise<void>;
  listUsers(orgId: number): Promise<UserRecord[]>;
  changeUser(
    userId: number,
    change: UserChange,
    administratorRoles: string[],
    audit: AuditContext,
  ): Promise<UserRecord | 'missing' | 'last_admin'>;
  replaceRoles(
    userId: number,
    roles: string[],
    administratorRoles: string[],
    audit: AuditContext,
  ): Promise<'updated' | 'missing' | 'last_admin'>;
}

const roleNames =
  'ARRAY(SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id ' +
  '      WHERE ur.user_id = u.id ORDER BY r.name) AS roles';
const principalFields = 'u.id, u.org_id AS "orgId", o.slug AS "orgSlug", ' + roleNames;
const activeUsers = ' FROM users u JOIN organizations o ON o.id = u.org_id WHERE u.active AND ';
const principalQuery = 'SELECT ' + principalFields + activeUsers;
const userQuery =
  'SELECT u.id, COALESCE(u.name, u.email) AS name, u.email, u.active, d.name AS department, ' +
  roleNames +
  ' FROM users u LEFT JOIN departments d ON d.org_id = u.org_id AND d.id = u.department_id ' +
  'WHERE u.org_id = $1';

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

  async saveRefresh(user: Principal, token: RefreshRecord, audit: AuditRequest): Promise<boolean> {
    return inTransaction(this.pool, async (client) => {
      const active = await client.query('SELECT id FROM users WHERE id = $1 AND active FOR SHARE', [
        user.id,
      ]);
      if (!active.rows.length) return false;
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
      return true;
    });
  }

  async rotateRefresh(
    hash: string,
    now: Date,
    replacement: RefreshRecord,
    audit: AuditRequest,
  ): Promise<Principal | undefined> {
    return inTransaction(this.pool, async (client) => {
      // Lock the user before consuming a token so deactivation can revoke every session.
      const { rows } = await client.query<{ user_id: number }>(
        'SELECT user_id FROM refresh_tokens WHERE token_hash = $1 AND expires_at > $2',
        [hash, now],
      );
      const token = rows[0];
      if (!token) return undefined;
      const { rows: users } = await client.query<Principal>(
        principalQuery + 'u.id = $1 FOR SHARE OF u',
        [token.user_id],
      );
      const user = users[0];
      if (!user) return undefined;
      const { rows: consumed } = await client.query<{ expires_at: Date }>(
        'DELETE FROM refresh_tokens WHERE token_hash = $1 AND expires_at > $2 RETURNING expires_at',
        [hash, now],
      );
      const original = consumed[0];
      if (!original) return undefined;
      await this.insertRefresh(client, user.id, replacement);
      await writeAudit(
        client,
        { ...audit, orgId: user.orgId, actorUserId: user.id },
        {
          action: 'auth.refresh',
          entityType: 'user',
          entityId: String(user.id),
          before: { expires_at: original.expires_at },
          after: { expires_at: replacement.expiresAt },
        },
      );
      return user;
    });
  }

  async revokeRefresh(hash: string, audit: AuditRequest): Promise<void> {
    await inTransaction(this.pool, async (client) => {
      const { rows } = await client.query<{ user_id: number; org_id: number }>(
        'DELETE FROM refresh_tokens t USING users u WHERE t.token_hash = $1 AND u.id = t.user_id ' +
          'RETURNING t.user_id, u.org_id',
        [hash],
      );
      const session = rows[0];
      if (!session) return;
      await writeAudit(
        client,
        { ...audit, orgId: session.org_id, actorUserId: session.user_id },
        {
          action: 'auth.logout',
          entityType: 'user',
          entityId: String(session.user_id),
          before: { session_active: true },
          after: { session_active: false },
        },
      );
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

  async listUsers(orgId: number): Promise<UserRecord[]> {
    return (await this.pool.query<UserRecord>(userQuery + ' ORDER BY u.name, u.id', [orgId])).rows;
  }

  async changeUser(
    userId: number,
    change: UserChange,
    administratorRoles: string[],
    audit: AuditContext,
  ): Promise<UserRecord | 'missing' | 'last_admin'> {
    return inTransaction(this.pool, async (client) => {
      await client.query('SELECT id FROM organizations WHERE id = $1 FOR UPDATE', [audit.orgId]);
      const {
        rows: [user],
      } = await client.query<UserRecord>(userQuery + ' AND u.id = $2 FOR NO KEY UPDATE OF u', [
        audit.orgId,
        userId,
      ]);
      if (!user || !user.active) return 'missing';
      const entity = { entityType: 'user', entityId: String(userId) };
      if ('department' in change) {
        if (user.department === change.department) return user;
        await client.query(
          'UPDATE users SET department_id = (SELECT id FROM departments WHERE org_id = $1 AND name = $3) ' +
            'WHERE org_id = $1 AND id = $2',
          [audit.orgId, userId, change.department],
        );
        await writeAudit(client, audit, {
          ...entity,
          action: 'user.department',
          before: { department: user.department },
          after: change,
        });
      } else {
        if (
          user.roles.some((role) => administratorRoles.includes(role)) &&
          !(await this.hasOtherAdministrator(client, audit.orgId, userId, administratorRoles))
        )
          return 'last_admin';
        await client.query('UPDATE users SET active = false WHERE org_id = $1 AND id = $2', [
          audit.orgId,
          userId,
        ]);
        await client.query('DELETE FROM refresh_tokens WHERE user_id = $1', [userId]);
        await writeAudit(client, audit, {
          ...entity,
          action: 'user.deactivate',
          before: { active: true },
          after: change,
        });
      }
      return { ...user, ...change };
    });
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
        !roles.some((role) => administratorRoles.includes(role)) &&
        !(await this.hasOtherAdministrator(client, audit.orgId, userId, administratorRoles))
      ) {
        return 'last_admin';
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

  private async hasOtherAdministrator(
    client: PoolClient,
    orgId: number,
    userId: number,
    roles: string[],
  ) {
    const { rows } = await client.query(
      'SELECT 1 FROM users u JOIN user_roles ur ON u.id = ur.user_id JOIN roles r ON r.id = ur.role_id ' +
        'WHERE u.org_id = $1 AND u.active AND u.id <> $2 AND r.name = ANY($3::text[]) LIMIT 1',
      [orgId, userId, roles],
    );
    return rows.length > 0;
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
