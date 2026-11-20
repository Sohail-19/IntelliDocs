import { json, Router } from 'express';
import type { CookieOptions, Request, RequestHandler, Response } from 'express';
import { rateLimit } from 'express-rate-limit';
import type { AuthTokens, UserChange } from '../../../../contracts/core-api.v1.js';

import { auditContext, auditRequest } from '../audit.js';
import { requestContext } from '../runtime.js';
import type { Permission, Principal } from './policy.js';
import { AuthError } from './service.js';
import type { AuthService, Credentials, Registration } from './service.js';

export type Guard = (...permissions: Permission[]) => RequestHandler;

export function principal(response: Response): Principal {
  const user = response.locals.principal as Principal | undefined;
  if (!user) throw new Error('An authenticated principal is required');
  return user;
}

function authorized(auth: AuthService, check: (user: Principal) => boolean): RequestHandler {
  return (request, response, next) => {
    void auth
      .authenticate(request.get('authorization'))
      .then((user) => {
        if (!check(user)) throw new AuthError(403, 'Access denied');
        response.locals.principal = user;
        const context = requestContext.getStore();
        if (context) context.actor = user.id;
        response.set('Cache-Control', 'private, no-store');
        next();
      })
      .catch((error: unknown) => {
        if (error instanceof AuthError) sendError(response, error);
        else next(error);
      });
  };
}

export function requireRole(auth: AuthService, ...roles: string[]): RequestHandler {
  return authorized(auth, (user) => roles.some((role) => user.roles.includes(role)));
}

export function requirePermission(auth: AuthService, ...permissions: Permission[]): RequestHandler {
  return authorized(auth, (user) =>
    permissions.some((permission) => auth.allows(user, permission)),
  );
}

function sendError(response: Response, error: AuthError): void {
  if (error.status === 401) response.set('WWW-Authenticate', 'Bearer');
  response.status(error.status).json({ error: error.message });
}

function bodyOf(request: Request): Record<string, unknown> {
  const body: unknown = request.body;
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    throw new AuthError(422, 'Expected a JSON object');
  }
  return body as Record<string, unknown>;
}

const refreshCookie = '__Host-intellidocs-refresh';
const cookieOptions: CookieOptions = {
  secure: true,
  httpOnly: true,
  sameSite: 'strict',
  path: '/',
};

function refreshToken(request: Request, production: boolean): string {
  if (production && !request.get('Origin')) throw new AuthError(403, 'Origin is required');
  const token = production
    ? request
        .get('Cookie')
        ?.split(';')
        .map((item) => item.trim())
        .find((item) => item.startsWith(`${refreshCookie}=`))
        ?.slice(refreshCookie.length + 1)
    : bodyOf(request).refresh_token;
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(token)) {
    throw new AuthError(401, 'Invalid refresh token');
  }
  return token;
}

function credentials(body: Record<string, unknown>, registering = false): Credentials {
  const { org_id, password } = body;
  const email = typeof body.email === 'string' ? body.email.trim().toLowerCase() : '';
  if (
    typeof org_id !== 'string' ||
    !/^[A-Za-z0-9_-]{1,128}$/.test(org_id) ||
    !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ||
    email.length > 320 ||
    typeof password !== 'string' ||
    password.length < (registering ? 12 : 1) ||
    password.length > 256
  )
    throw new AuthError(422, 'Invalid organization, email, or password format');
  return { org_id, email, password };
}

function registration(body: Record<string, unknown>): Registration {
  const { name, department, roles, bootstrap_token } = body;
  if (
    typeof name !== 'string' ||
    !name.trim() ||
    name.length > 200 ||
    (department !== undefined && typeof department !== 'string') ||
    (roles !== undefined &&
      (!Array.isArray(roles) || roles.length > 32 || roles.some((r) => typeof r !== 'string'))) ||
    (bootstrap_token !== undefined && typeof bootstrap_token !== 'string')
  ) {
    throw new AuthError(422, 'Invalid registration fields');
  }
  return {
    ...credentials(body, true),
    name: name.trim(),
    ...(department === undefined ? {} : { department }),
    ...(roles === undefined ? {} : { roles: roles as string[] }),
    ...(bootstrap_token === undefined ? {} : { bootstrap_token }),
  };
}

function handle(run: (request: Request, response: Response) => Promise<void>): RequestHandler {
  return (request, response, next) => {
    void run(request, response).catch((error: unknown) => {
      if (error instanceof AuthError) sendError(response, error);
      else next(error);
    });
  };
}
function userId(request: Request): number {
  const value = request.params.id;
  const id = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : 0;
  if (!Number.isSafeInteger(id) || id <= 0 || id > 2147483647) {
    throw new AuthError(422, 'Invalid user ID');
  }
  return id;
}

export function userRouter(auth: AuthService, guard: Guard): Router {
  const router = Router();
  const manage = guard('users:roles');
  router.use('/api/users', manage, (_request, response, next) => {
    response.set('Cache-Control', 'no-store');
    next();
  });
  router.get(
    '/api/users',
    handle(async (_request, response) => {
      response.json(await auth.listUsers(principal(response)));
    }),
  );
  router.patch(
    '/api/users/:id',
    handle(async (request, response) => {
      const id = userId(request);
      const body = bodyOf(request);
      const { department, active } = body;
      let change: UserChange;
      if (
        Object.keys(body).length === 1 &&
        (department === null || typeof department === 'string')
      ) {
        change = { department };
      } else if (Object.keys(body).length === 1 && active === false) {
        change = { active };
      } else {
        throw new AuthError(422, 'Provide a department or deactivate the user');
      }
      const actor = principal(response);
      const user = await auth.changeUser(id, change, actor, auditContext(request, response, actor));
      response.json({ user });
    }),
  );
  router.put(
    '/api/users/:id/roles',
    handle(async (request, response) => {
      const id = userId(request);
      const { roles } = bodyOf(request);
      if (
        !Array.isArray(roles) ||
        roles.length > 32 ||
        roles.some((role) => typeof role !== 'string')
      ) {
        throw new AuthError(422, 'Invalid role list');
      }
      const actor = principal(response);
      await auth.replaceRoles(id, roles as string[], actor, auditContext(request, response, actor));
      response.sendStatus(204);
    }),
  );
  return router;
}

export function authRouter(auth: AuthService, production = false): Router {
  const router = Router();
  const sendTokens = (response: Response, tokens: AuthTokens & { refresh_token: string }) => {
    if (!production) {
      response.json(tokens);
      return;
    }
    const { refresh_token, ...payload } = tokens;
    response
      .cookie(refreshCookie, refresh_token, {
        ...cookieOptions,
        maxAge: auth.settings.refreshSeconds * 1000,
      })
      .json(payload);
  };
  router.use(
    (_request, response, next) => {
      response.set('Cache-Control', 'no-store');
      next();
    },
    rateLimit({
      windowMs: 60_000,
      limit: auth.settings.rateLimit,
      standardHeaders: 'draft-8',
      legacyHeaders: false,
      // Proxy trust is explicitly configured by the operator, outside visitor control.
      validate: { xForwardedForHeader: false, forwardedHeader: false },
      message: { error: 'Too many authentication requests' },
    }),
    json({ limit: '8kb', inflate: false }),
  );
  // Registration checks bootstrap eligibility or users:register for the requested organization.
  router.post(
    '/register',
    handle(async (request, response) => {
      const input = registration(bodyOf(request));
      const header = request.get('authorization');
      await auth.register(
        input,
        auditRequest(request, response),
        header === undefined ? undefined : await auth.authenticate(header),
      );
      response.status(201).json({ email: input.email, org_id: input.org_id });
    }),
  );
  // Login authenticates credentials. Refresh authenticates the single-use refresh token.
  router.post(
    '/login',
    handle(async (request, response) => {
      sendTokens(
        response,
        await auth.login(credentials(bodyOf(request)), auditRequest(request, response)),
      );
    }),
  );
  router.post(
    '/refresh',
    handle(async (request, response) => {
      sendTokens(
        response,
        await auth.refresh(refreshToken(request, production), auditRequest(request, response)),
      );
    }),
  );
  router.get(
    '/me',
    authorized(auth, () => true),
    (_request, response) => {
      response.json(auth.profile(principal(response)));
    },
  );
  router.post(
    '/logout',
    handle(async (request, response) => {
      await auth.logout(refreshToken(request, production), auditRequest(request, response));
      if (production) response.clearCookie(refreshCookie, cookieOptions);
      response.sendStatus(204);
    }),
  );
  return router;
}
