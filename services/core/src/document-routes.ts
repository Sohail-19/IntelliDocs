import { Router } from 'express';
import type { DocumentUploadOptions } from '../../../contracts/core-api.v1.js';
import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import type { OrgConfig } from './config/loader.js';
import type { DocumentReader } from './documents.js';

export interface DocumentBrowser {
  reader: DocumentReader;
  configs: ReadonlyMap<
    string,
    Pick<OrgConfig, 'departments' | 'document_types' | 'upload_defaults' | 'languages'>
  >;
}

export function documentRouter(
  { reader, configs }: DocumentBrowser,
  guard: Guard,
  maxBytes: number,
) {
  const router = Router();
  const access = guard('documents:upload');
  router.get('/api/documents/upload-options', access, (_request, response) => {
    const config = configs.get(principal(response).orgSlug);
    if (!config) {
      response.status(404).json({ error: 'Organization upload options are unavailable' });
      return;
    }
    const options: DocumentUploadOptions = {
      departments: config.departments,
      document_types: config.document_types,
      upload_defaults: config.upload_defaults ?? {},
      max_upload_bytes: maxBytes,
    };
    response.set('Cache-Control', 'no-store').json(options);
  });
  router.get('/api/documents', access, (_request, response, next) => {
    void reader
      .list(principal(response).orgId)
      .then((documents) => {
        response.set('Cache-Control', 'no-store').json({ documents });
      })
      .catch(next);
  });
  router.get(
    '/api/documents/:id',
    guard('documents:upload', 'documents:search', 'documents:answer'),
    (request, response, next) => {
      const id = request.params.id;
      if (typeof id !== 'string' || id.length > 255 || id.includes('\0')) {
        response.status(400).json({ error: 'Invalid document ID' });
        return;
      }
      void reader
        .get(principal(response).orgId, id)
        .then((document) => {
          if (!document) {
            response.status(404).json({ error: 'Document not found' });
            return;
          }
          response.set('Cache-Control', 'no-store').json({ document });
        })
        .catch(next);
    },
  );
  return router;
}
