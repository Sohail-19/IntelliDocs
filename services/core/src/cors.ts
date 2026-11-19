import type { RequestHandler } from 'express';

export function configuredCors(value = process.env.CORE_CORS_ORIGINS ?? ''): RequestHandler {
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
  return (request, response, next) => {
    response.vary('Origin');
    const origin = request.get('Origin');
    if (!origin || !allowed.has(origin)) {
      next();
      return;
    }
    response.set('Access-Control-Allow-Origin', origin);
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
