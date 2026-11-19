import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

import { argon2id, hash, verify } from 'argon2';
import { errors, jwtVerify, SignJWT } from 'jose';

import type {
  AuthCredentials,
  AuthProfile,
  AuthTokens,
  CoreAdminApi,
  UserChange,
} from '../../../../contracts/core-api.v1.js';
import type { AuditContext, AuditRequest } from '../audit.js';
import { permits } from './policy.js';
import type { OrgPolicy, Permission, Principal } from './policy.js';
import type { AuthRepository } from './repository.js';

const issuer = 'intellidocs-core';
const audience = 'intellidocs-api';
const hashOptions = { type: argon2id, memoryCost: 19456, timeCost: 2, parallelism: 1 } as const;

export class AuthError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function hashToken(raw: string): string {
  return createHash('sha256').update(raw).digest('hex');
}

export interface AuthSettings {
  jwtSecret: Uint8Array;
  bootstrapHash: string;
  accessSeconds: number;
  refreshSeconds: number;
  rateLimit: number;
}

function integerSetting(name: string, fallback: number, maximum: number): number {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isSafeInteger(value) || value <= 0 || value > maximum) {
    throw new Error('Invalid ' + name);
  }
  return value;
}

export function authSettingsFromEnv(): AuthSettings {
  const secret = process.env.AUTH_JWT_SECRET ?? '';
  const bootstrap = process.env.AUTH_BOOTSTRAP_TOKEN ?? '';
  if (Buffer.byteLength(secret) < 32 || Buffer.byteLength(bootstrap) < 32 || secret === bootstrap) {
    throw new Error(
      'Set distinct AUTH_JWT_SECRET and AUTH_BOOTSTRAP_TOKEN values of at least 32 bytes',
    );
  }
  return {
    jwtSecret: new TextEncoder().encode(secret),
    bootstrapHash: hashToken(bootstrap),
    accessSeconds: integerSetting('AUTH_ACCESS_TTL_SECONDS', 900, 3600),
    refreshSeconds: integerSetting('AUTH_REFRESH_TTL_SECONDS', 604800, 2592000),
    rateLimit: integerSetting('AUTH_RATE_LIMIT_PER_MINUTE', 15, 1000),
  };
}

export type Credentials = AuthCredentials;

export interface Registration extends Credentials {
  name: string;
  department?: string;
  roles?: string[];
  bootstrap_token?: string;
}

export class AuthService {
  private constructor(
    private readonly repository: AuthRepository,
    private readonly policies: ReadonlyMap<string, OrgPolicy>,
    public readonly settings: AuthSettings,
    private readonly dummyHash: string,
    private readonly now: () => Date,
  ) {}

  static async create(
    repository: AuthRepository,
    policies: ReadonlyMap<string, OrgPolicy>,
    settings: AuthSettings,
    now: () => Date = () => new Date(),
  ): Promise<AuthService> {
    return new AuthService(
      repository,
      policies,
      settings,
      await hash(randomBytes(32), hashOptions),
      now,
    );
  }

  allows(user: Principal, permission: Permission): boolean {
    return permits(this.policies.get(user.orgSlug), user.roles, permission);
  }

  profile(user: Principal): AuthProfile {
    const roles = this.policies.get(user.orgSlug)?.roles;
    return {
      user_id: user.id,
      org_id: user.orgSlug,
      roles: user.roles,
      permissions: [...new Set(user.roles.flatMap((role) => roles?.get(role) ?? []))].sort(),
    };
  }

  async register(input: Registration, audit: AuditRequest, actor?: Principal): Promise<void> {
    const org = this.policies.get(input.org_id);
    const administratorAuthorized =
      actor?.orgSlug === input.org_id && this.allows(actor, 'users:register');
    const bootstrapAuthorized =
      input.bootstrap_token !== undefined &&
      timingSafeEqual(
        Buffer.from(hashToken(input.bootstrap_token), 'hex'),
        Buffer.from(this.settings.bootstrapHash, 'hex'),
      );
    if (!org || (!administratorAuthorized && !bootstrapAuthorized)) {
      throw new AuthError(403, 'Registration is not allowed');
    }
    const roles = [...new Set(input.roles ?? [org.registrationRole])];
    if (!roles.length || roles.some((role) => !org.roles.has(role))) {
      throw new AuthError(422, 'Invalid roles for this organization');
    }
    if (input.department !== undefined && !org.departments.includes(input.department)) {
      throw new AuthError(422, 'Invalid department for this organization');
    }
    const outcome = await this.repository.register({
      email: input.email,
      name: input.name,
      ...(input.department === undefined ? {} : { department: input.department }),
      roles,
      org,
      administratorAuthorized,
      bootstrapAuthorized,
      audit,
      actorUserId: administratorAuthorized ? (actor?.id ?? null) : null,
      passwordHash: await hash(input.password, hashOptions),
    });
    if (outcome === 'duplicate') throw new AuthError(409, 'An account already exists');
    if (outcome === 'forbidden') throw new AuthError(403, 'Registration is not allowed');
  }

  async login(input: Credentials, audit: AuditRequest): Promise<AuthTokens> {
    const user = await this.repository.account(input.org_id, input.email);
    const valid = await verify(user?.passwordHash ?? this.dummyHash, input.password);
    if (!user || !valid || !this.policies.has(user.orgSlug)) {
      throw new AuthError(401, 'Invalid credentials');
    }
    const now = this.now();
    const refresh = this.newRefresh(now);
    if (!(await this.repository.saveRefresh(user, refresh.record, audit))) {
      throw new AuthError(401, 'Invalid credentials');
    }
    return this.issue(user, refresh.raw, now);
  }

  async refresh(raw: string, audit: AuditRequest): Promise<AuthTokens> {
    const now = this.now();
    const refresh = this.newRefresh(now);
    const user = await this.repository.rotateRefresh(hashToken(raw), now, refresh.record, audit);
    if (!user || !this.policies.has(user.orgSlug))
      throw new AuthError(401, 'Invalid refresh token');
    return this.issue(user, refresh.raw, now);
  }

  async logout(raw: string, audit: AuditRequest): Promise<void> {
    await this.repository.revokeRefresh(hashToken(raw), audit);
  }

  async replaceRoles(
    userId: number,
    names: string[],
    actor: Principal,
    audit: AuditContext,
  ): Promise<void> {
    const policy = this.userAdminPolicy(actor);
    const roles = [...new Set(names)].sort();
    if (!roles.length || roles.some((role) => !policy.roles.has(role))) {
      throw new AuthError(422, 'Invalid roles for this organization');
    }
    const outcome = await this.repository.replaceRoles(
      userId,
      roles,
      this.administratorRoles(policy),
      audit,
    );
    if (outcome !== 'updated') this.rejectUserChange(outcome);
  }

  async listUsers(actor: Principal): Promise<CoreAdminApi['/api/users']['result']> {
    const policy = this.userAdminPolicy(actor);
    return {
      users: await this.repository.listUsers(actor.orgId),
      departments: policy.departments,
      roles: [...policy.roles.keys()].sort(),
    };
  }

  async changeUser(userId: number, change: UserChange, actor: Principal, audit: AuditContext) {
    const policy = this.userAdminPolicy(actor);
    if (
      'department' in change &&
      change.department !== null &&
      !policy.departments.includes(change.department)
    ) {
      throw new AuthError(422, 'Invalid department for this organization');
    }
    const user = await this.repository.changeUser(
      userId,
      change,
      this.administratorRoles(policy),
      audit,
    );
    if (typeof user === 'string') this.rejectUserChange(user);
    return user;
  }

  private userAdminPolicy(actor: Principal) {
    const policy = this.policies.get(actor.orgSlug);
    if (!policy || !this.allows(actor, 'users:roles')) throw new AuthError(403, 'Access denied');
    return policy;
  }

  private administratorRoles(policy: OrgPolicy): string[] {
    return [...policy.roles]
      .filter(([, grants]) => grants.includes('users:roles'))
      .map(([role]) => role);
  }

  private rejectUserChange(outcome: 'missing' | 'last_admin'): never {
    if (outcome === 'missing') throw new AuthError(404, 'User not found');
    throw new AuthError(409, 'At least one active role administrator is required');
  }

  async authenticate(header: string | undefined): Promise<Principal> {
    const token = header?.match(/^Bearer ([A-Za-z0-9_.-]+)$/i)?.[1];
    if (!token) throw new AuthError(401, 'Valid access token required');
    let subject: string | undefined;
    try {
      const { payload } = await jwtVerify(token, this.settings.jwtSecret, {
        algorithms: ['HS256'],
        issuer,
        audience,
        currentDate: this.now(),
        requiredClaims: ['exp', 'iat', 'sub'],
        typ: 'at+jwt',
      });
      subject = payload.sub;
    } catch (error) {
      if (error instanceof errors.JOSEError)
        throw new AuthError(401, 'Valid access token required');
      throw error;
    }
    const id = Number(subject);
    if (!Number.isSafeInteger(id) || id <= 0)
      throw new AuthError(401, 'Valid access token required');
    const user = await this.repository.principal(id);
    if (!user || !this.policies.has(user.orgSlug))
      throw new AuthError(401, 'Valid access token required');
    return user;
  }

  private newRefresh(now: Date) {
    const raw = randomBytes(32).toString('base64url');
    return {
      raw,
      record: {
        hash: hashToken(raw),
        expiresAt: new Date(now.getTime() + this.settings.refreshSeconds * 1000),
      },
    };
  }

  private async issue(user: Principal, refresh: string, now: Date): Promise<AuthTokens> {
    const issued = Math.floor(now.getTime() / 1000);
    const access = await new SignJWT({})
      .setProtectedHeader({ alg: 'HS256', typ: 'at+jwt' })
      .setIssuer(issuer)
      .setAudience(audience)
      .setSubject(String(user.id))
      .setIssuedAt(issued)
      .setExpirationTime(issued + this.settings.accessSeconds)
      .sign(this.settings.jwtSecret);
    return {
      access_token: access,
      refresh_token: refresh,
      token_type: 'Bearer',
      expires_in: this.settings.accessSeconds,
    };
  }
}
