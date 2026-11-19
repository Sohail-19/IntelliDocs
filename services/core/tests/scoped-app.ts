import type { AuditContext } from '../src/audit.js';
import { createApp } from '../src/app.js';
import type { Principal } from '../src/auth/policy.js';

const identity: Principal = { id: 1, orgId: 42, orgSlug: 'example', roles: [] };

export const fixtureAudit: AuditContext = {
  orgId: identity.orgId,
  actorUserId: identity.id,
  requestId: 'test-request',
  ip: null,
};

// Route-specific tests use a fixture identity. The auth suite exercises the real guards.
export function scopedApp(dependencies: NonNullable<Parameters<typeof createApp>[0]>) {
  return createApp(dependencies, {
    guard: () => (_request, response, next) => {
      response.locals.principal = identity;
      next();
    },
  });
}
