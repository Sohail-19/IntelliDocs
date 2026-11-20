import { randomUUID } from 'node:crypto';
import { orgPolicy } from './auth/policy.js';
import { PostgresAuthRepository } from './auth/repository.js';
import { hashPassword } from './auth/service.js';
import { loadNamedOrgConfigs } from './config/loader.js';
import { createDatabasePool } from './database.js';

interface SeedAccount {
  org_id: string;
  email: string;
  password: string;
  name: string;
  role?: string;
}

function isAccount(value: unknown): value is SeedAccount {
  if (!value || typeof value !== 'object') return false;
  const { org_id, email, password, name, role } = value as Partial<SeedAccount>;
  return (
    typeof org_id === 'string' &&
    /^[A-Za-z0-9_-]+$/.test(org_id) &&
    typeof email === 'string' &&
    email.length <= 320 &&
    /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) &&
    typeof password === 'string' &&
    password.length >= 12 &&
    password.length <= 256 &&
    typeof name === 'string' &&
    Boolean(name.trim()) &&
    name.length <= 200 &&
    (role === undefined || typeof role === 'string')
  );
}

async function seed(): Promise<number> {
  const input: unknown = JSON.parse(process.env.DEMO_SEED_ACCOUNTS_JSON ?? 'null');
  if (!Array.isArray(input) || !input.length || !input.every(isAccount))
    throw new Error('Invalid seed accounts');
  const orgs = new Map(
    (await loadNamedOrgConfigs(process.env.ORG_CONFIG_DIR)).map(({ slug, config }) => [
      slug,
      orgPolicy(slug, config),
    ]),
  );
  const accounts = input.map((account) => {
    const org = orgs.get(account.org_id);
    const role = account.role ?? org?.registrationRole;
    const permissions = role ? org?.roles.get(role) : undefined;
    if (
      !org ||
      !role ||
      !permissions ||
      ['users:register', 'users:roles'].some((permission) => permissions.includes(permission))
    )
      throw new Error('Seed users must have a configured non-administration role');
    return { ...account, org, role };
  });
  const pool = createDatabasePool();
  try {
    const repo = new PostgresAuthRepository(pool);
    const requestId = randomUUID();
    let created = 0;
    for (const { email, name, org, role, password } of accounts) {
      const normalized = email.toLowerCase();
      if (await repo.account(org.slug, normalized)) continue;
      const outcome = await repo.register({
        email: normalized,
        name: name.trim(),
        org,
        passwordHash: await hashPassword(password),
        roles: [role],
        bootstrapAuthorized: false,
        administratorAuthorized: true,
        audit: { requestId, ip: null },
        actorUserId: null,
      });
      if (outcome === 'forbidden')
        throw new Error('Initialize and bootstrap the organization first');
      if (outcome === 'created') created++;
    }
    return created;
  } finally {
    await pool.end();
  }
}

void seed()
  .then((created) => console.log(JSON.stringify({ event: 'demo_accounts_seeded', created })))
  .catch(() => {
    console.error(
      JSON.stringify({
        event: 'demo_account_seed_failed',
        message:
          'Check the seed input, configured roles, and that every organization is bootstrapped.',
      }),
    );
    process.exitCode = 1;
  });
