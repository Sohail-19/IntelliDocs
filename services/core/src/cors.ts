import type { RequestHandler } from 'express';

export function configuredCors(
  value = process.env.CORE_CORS_ORIGINS ?? '',
  production = false,
): RequestHandler {
  const allowed = new Set(
    value
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean)
      .map((entry) => {
        const url = URL.canParse(entry) ? new URL(entry) : null;
        if (
          !url ||
          !['http:', 'https:'].includes(url.protocol) ||
          (production && url.protocol !== 'https:') ||
          url.pathname !== '/' ||
          url.username ||
          url.password ||
          url.search ||
          url.hash
        ) {
          throw new Error('CORE_CORS_ORIGINS must contain HTTP origins');
        }
        return url.origin;
      }),
  );
  if (production && !allowed.size)
    throw new Error('CORE_CORS_ORIGINS requires explicit HTTPS origins');
  return (request, response, next) => {
    response.vary('Origin');
    const origin = request.get('Origin');
    if (origin && production && !allowed.has(origin)) {
      response.status(403).json({ error: 'Origin is not allowed' });
      return;
    }
    if (!origin || !allowed.has(origin)) {
      next();
      return;
    }
    response.set('Access-Control-Allow-Origin', origin);
    if (production) response.set('Access-Control-Allow-Credentials', 'true');
    if (request.method === 'OPTIONS' && request.get('Access-Control-Request-Method')) {
      response.set({
        'Access-Control-Allow-Methods': 'GET, HEAD, POST, PUT, PATCH, OPTIONS',
        'Access-Control-Allow-Headers': 'Authorization, Content-Type',
      });
      response.sendStatus(204);
      return;
    }
    next();
  };
}
