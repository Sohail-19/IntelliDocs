import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { Router } from 'express';
import type { SearchOptions } from '../../../contracts/core-api.v1.js';
import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import type { Principal } from './auth/policy.js';
import type { DocumentBrowser } from './document-routes.js';
import { IntelligenceError } from './intelligence/client.js';
import type { IntelligenceClient } from './intelligence/client.js';

export function sourceRouter(
  { reader, configs }: DocumentBrowser,
  guard: Guard,
  pdfSource?: IntelligenceClient['pdfSource'],
  canReadSource?: (actor: Principal, docId: string) => Promise<boolean>,
) {
  const router = Router();
  const access = guard('documents:search', 'documents:answer');
  router.get('/api/search/options', access, (_request, response) => {
    const config = configs.get(principal(response).orgSlug);
    if (!config) {
      response.status(404).json({ error: 'Organization search options are unavailable' });
      return;
    }
    const options: SearchOptions = {
      departments: config.departments,
      document_types: config.document_types,
      languages: config.languages,
    };
    response.set('Cache-Control', 'no-store').json(options);
  });
  if (pdfSource) {
    const sourceAccess = canReadSource
      ? guard('documents:search', 'documents:answer', 'tasks:read', 'obligations:review')
      : access;
    router.get('/api/documents/:id/source', sourceAccess, (request, response) => {
      const id = request.params.id;
      const actor = principal(response);
      const { orgId, orgSlug } = actor;
      if (typeof id !== 'string' || id.length > 255 || id.includes('\0')) {
        response.status(400).json({ error: 'Invalid document ID' });
        return;
      }
      void (async () => {
        const document = await reader.get(orgId, id);
        if (!document || (canReadSource && !(await canReadSource(actor, id)))) {
          response.status(404).json({ error: 'Source not found' });
          return;
        }
        if (document.mime_type && document.mime_type !== 'application/pdf') {
          response.status(415).json({ error: 'This source does not have an original PDF' });
          return;
        }
        const upstream = await pdfSource(id, orgSlug);
        if (!upstream.body || upstream.headers.get('content-type') !== 'application/pdf') {
          await upstream.body?.cancel();
          response.status(502).json({ error: 'The source service did not return a PDF' });
          return;
        }
        response.set({
          'Content-Type': 'application/pdf',
          'Content-Disposition': 'inline',
          'Cache-Control': 'private, no-store',
        });
        await pipeline(Readable.fromWeb(upstream.body), response);
      })().catch((error: unknown) => {
        if (response.destroyed || response.headersSent) {
          response.destroy();
        } else if (
          error instanceof IntelligenceError &&
          (error.status === 404 || error.status === 415)
        ) {
          response.status(error.status).json({
            error:
              error.status === 404
                ? 'Source not found'
                : 'This source does not have an original PDF',
          });
        } else {
          response.status(502).json({ error: 'Source PDF could not be loaded' });
        }
      });
    });
  }
  return router;
}
