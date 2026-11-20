import { AsyncLocalStorage } from 'node:async_hooks';
import { randomUUID } from 'node:crypto';
import type { RequestHandler } from 'express';

export function flag(name: string, fallback: boolean): boolean {
  const value = process.env[name] ?? String(fallback);
  if (value !== 'true' && value !== 'false') throw new Error(`Invalid ${name}`);
  return value === 'true';
}

export function limit(name: string, fallback: number, minimum = 1, maximum = 2147483647): number {
  const raw = process.env[name] ?? String(fallback);
  const value = Number(raw);
  if (!/^\d+$/.test(raw) || !Number.isSafeInteger(value) || value < minimum || value > maximum)
    throw new Error(`Invalid ${name}`);
  return value;
}

export function strongSecret(raw: string): boolean {
  return (
    Buffer.byteLength(raw) >= 32 &&
    new Set(raw).size >= 10 &&
    !/your[_ -]|placeholder|change.?me|replace.?me|example|default|development/i.test(raw)
  );
}

export function runtimeFromEnv() {
  const profile = process.env.INTELLIDOCS_ENV ?? 'development';
  if (!['development', 'test', 'production'].includes(profile))
    throw new Error('Invalid INTELLIDOCS_ENV');
  const production = profile === 'production';
  const demo = flag('DEMO_MODE', false);
  const internalKey = process.env.INTELLIDOCS_INTERNAL_KEY ?? '';
  if ((production || demo || internalKey) && !strongSecret(internalKey))
    throw new Error('INTELLIDOCS_INTERNAL_KEY requires a strong shared value');
  if (
    internalKey &&
    [process.env.AUTH_JWT_SECRET, process.env.AUTH_BOOTSTRAP_TOKEN].includes(internalKey)
  )
    throw new Error('INTELLIDOCS_INTERNAL_KEY must be different from auth secrets');
  return {
    production,
    demo,
    internalKey,
    jsonBytes: limit('CORE_MAX_JSON_BYTES', 32768),
    proxyHops: limit('CORE_TRUST_PROXY_HOPS', 0, 0, 10),
    uploadsPerUser: demo ? limit('DEMO_UPLOADS_PER_USER_DAY', 5, 0) : 0,
    uploadsGlobal: demo ? limit('DEMO_UPLOADS_GLOBAL_DAY', 30, 0) : 0,
    concurrentUploads: demo ? limit('DEMO_MAX_CONCURRENT_UPLOADS', 2) : 0,
    retentionDays: demo ? limit('DEMO_RETENTION_DAYS', 7, 1, 3650) : 0,
    sweepMs: demo ? limit('DEMO_SWEEP_INTERVAL_SECONDS', 3600, 1, 86400) * 1000 : 0,
  };
}

export type Runtime = ReturnType<typeof runtimeFromEnv>;
export const requestContext = new AsyncLocalStorage<{ id: string; actor?: number }>();

export function logEvent(
  event:
    | 'request'
    | 'server_started'
    | 'startup_failed'
    | 'demo_cleanup_failed'
    | 'task_sweep_failed'
    | 'database_pool_error',
  fields: { request_id?: string; method?: string; status?: number } = {},
): void {
  console.log(JSON.stringify({ service: 'core', event, ...fields }));
}

export const requestLifecycle: RequestHandler = (request, response, next) => {
  const id = randomUUID();
  response.locals.requestId = id;
  response.set('X-Request-ID', id);
  const method = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'].includes(
    request.method,
  )
    ? request.method
    : 'OTHER';
  response.once('finish', () =>
    logEvent('request', { request_id: id, method, status: response.statusCode }),
  );
  requestContext.run({ id }, next);
};
