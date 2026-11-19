import type { Permission } from '../../../../contracts/core-api.v1.js';
import type { OrgConfig } from '../config/loader.js';

export type { Permission } from '../../../../contracts/core-api.v1.js';

export interface OrgPolicy {
  slug: string;
  departments: string[];
  roles: Map<string, readonly string[]>;
  bootstrapRole: string;
  registrationRole: string;
}

export interface Principal {
  id: number;
  orgId: number;
  orgSlug: string;
  roles: string[];
}

export function orgPolicy(slug: string, config: OrgConfig): OrgPolicy | undefined {
  if (!config.access) return undefined;
  const { bootstrap_role, registration_role, role_permissions } = config.access;
  const roles = new Map(
    Object.keys(config.roles ?? {}).map((role) => [
      role,
      Object.hasOwn(role_permissions, role) ? (role_permissions[role] ?? []) : [],
    ]),
  );
  if (
    !/^[A-Za-z0-9_-]+$/.test(slug) ||
    !roles.get(bootstrap_role)?.includes('users:register') ||
    !roles.has(registration_role) ||
    roles.get(registration_role)?.includes('users:register') ||
    Object.keys(role_permissions).some((role) => !roles.has(role))
  ) {
    throw new Error('Invalid access configuration for organization ' + slug);
  }
  return {
    slug,
    departments: config.departments,
    roles,
    bootstrapRole: bootstrap_role,
    registrationRole: registration_role,
  };
}

export function permits(
  policy: OrgPolicy | undefined,
  roles: string[],
  permission: Permission,
): boolean {
  return roles.some((role) => policy?.roles.get(role)?.includes(permission));
}
